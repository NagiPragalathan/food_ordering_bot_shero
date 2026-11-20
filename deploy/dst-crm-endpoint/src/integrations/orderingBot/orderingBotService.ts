/**
 * WhatsApp ordering bot -> CRM (DST India's "Hungry Now? Order Food").
 *
 * The ordering bot (smo.shero.us/dst) posts one event per thing that happens
 * to a customer; this file writes it into the CRM the way the CRM's own code
 * does, so the Lead Pipeline & Tracker, Leads, Customers and Orders pages show
 * it with nothing changed on their side:
 *
 *   lead.stage     the customer moved in the ordering funnel
 *                  -> the lead's pipeline stage (gallaboxStageKey + history,
 *                     as gallaboxWebhookService does) and CRM stage
 *   nudge          a follow-up message the bot sent, or the button tapped on it
 *                  -> a WhatsApp activity, an outbound message log, and the
 *                     RNR / Lost Feedback stage where the bot says so
 *   order.paid     a paid order
 *                  -> the Customer (contact), their address and vertical, the
 *                     Order with its line items, ORDER + PAYMENT activities
 *   order.status   sent to kitchen / out for delivery / delivered / cancelled
 *                  -> the Order's status, the pipeline stage, an activity
 *   order.feedback the customer's rating after delivery -> a note
 *
 * Additive only: every Order Now customer gets their own lead in the
 * "orderNow" vertical (found again by the bot's customer id), so a lead in
 * another vertical (a party order, a subscription) is never moved by the bot.
 * Nothing the team typed is overwritten: contact details are only filled in
 * when empty, and a lead the team marked WON stays WON.
 *
 * Each event runs in one transaction. `dryRun` runs it and rolls it back, to
 * test against the real database without leaving anything behind. Replaying
 * an event (the bot retries until it gets a 2xx) changes nothing twice.
 */
import crypto from "crypto";
import bcrypt from "bcrypt";
import { Prisma } from "@prisma/client";
import type { ActivityType, Lead, LeadStage, OrderStatus } from "@prisma/client";
import { prisma } from "../../config/prisma";

type Tx = Prisma.TransactionClient;

export const INTEGRATION_VERSION = "2026-11-20.1";
export const VERTICAL_KEY = "orderNow";
const VERTICAL_LABEL = "Order Now";
const VERTICAL_SORT_ORDER = 5;
const LEAD_SOURCE = "WhatsApp Ordering Bot";
const EXTERNAL_SOURCE = "ORDERING_BOT";
// The orders' "placed by": a system user that cannot sign in (inactive, with
// a random password nobody knows). authService refuses inactive users.
export const BOT_USER_EMAIL = "whatsapp-ordering-bot@system.invalid";
const BOT_USER_NAME = "WhatsApp Ordering Bot (system)";

const EVENTS = new Set(["lead.stage", "nudge", "order.paid", "order.status", "order.feedback"]);
const CRM_STAGES: ReadonlySet<string> = new Set(["NEW", "CONTACTED", "IN_DISCUSSION", "QUOTATION", "WON", "LOST"]);
const ORDER_STATUSES: ReadonlySet<string> = new Set(["PENDING", "CONFIRMED", "DELIVERED", "CANCELLED"]);
const MAX_ITEMS = 200;

export class OrderingBotError extends Error {
  constructor(message: string, readonly status = 400) {
    super(message);
    this.name = "OrderingBotError";
  }
}

interface BotCustomer {
  id: string;
  phone: string;
  name: string | null;
  email: string | null;
  address: string | null;
  pincode: string | null;
}

interface BotStage {
  ours: string | null;
  pipeline: string | null;
  crm: LeadStage | null;
  note: string | null;
}

interface BotNudge {
  kind: string;
  attempt: number;
  template: string;
  status: string;
  reply: string | null;
  bucket: string | null;
  providerMessageId: string | null;
  summary: string | null;
}

interface BotItem {
  name: string;
  sku: string | null;
  unit: string | null;
  quantity: number;
  rate: string;
  amount: string;
}

interface BotOrder {
  number: string;
  stage: string | null;
  crmStatus: OrderStatus | null;
  items: BotItem[];
  subtotal: string;
  deliveryCharge: string;
  discount: string;
  discountCode: string | null;
  tax: string;
  total: string;
  deliveryDate: Date | null;
  slotLabel: string | null;
  address: string | null;
  pincode: string | null;
  latitude: number | null;
  longitude: number | null;
  kitchen: string | null;
  paymentReference: string | null;
  instructions: string | null;
  refundAmount: string | null;
  feedback: string | null;
}

export interface BotEvent {
  id: string;
  event: string;
  occurredAt: Date;
  customer: BotCustomer;
  stage: BotStage | null;
  nudge: BotNudge | null;
  order: BotOrder | null;
}

export interface EventResult {
  leadId: string;
  leadCode: string;
  customerId?: string;
  orderId?: string;
  applied: string[];
}

class DryRunRollback extends Error {
  constructor(readonly result: EventResult) {
    super("dry run: rolled back");
  }
}

// --- reading the event --------------------------------------------------------------------
function text(value: unknown, max: number): string | null {
  if (value === null || value === undefined) return null;
  const s = String(value).trim();
  return s ? s.slice(0, max) : null;
}

function money(value: unknown, field: string): string {
  if (value === null || value === undefined || value === "") return "0.00";
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0 || n > 1e9) throw new OrderingBotError(`${field} must be an amount`);
  return n.toFixed(2);
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function dateOrNull(value: unknown): Date | null {
  if (!value) return null;
  const d = new Date(String(value));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** The last ten digits, as the CRM's own normalizePhone matches numbers. */
export function lastTenDigits(phone: string): string {
  const digits = phone.replace(/\D/g, "");
  return digits.length > 10 ? digits.slice(-10) : digits;
}

function displayPhone(phone: string): string {
  return `+${phone.replace(/\D/g, "")}`;
}

function looksLikePhone(value: string | null | undefined): boolean {
  return !value || !value.trim() || /^\+?[\d\s-]{7,}$/.test(value.trim());
}

function parseStage(raw: unknown): BotStage | null {
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Record<string, unknown>;
  const crm = text(s.crm, 20);
  if (crm && !CRM_STAGES.has(crm)) throw new OrderingBotError(`stage.crm must be one of ${[...CRM_STAGES].join(", ")}`);
  return {
    ours: text(s.ours, 60),
    pipeline: text(s.pipeline, 40),
    crm: (crm as LeadStage | null) ?? null,
    note: text(s.note, 1000),
  };
}

function parseOrder(raw: unknown): BotOrder {
  if (!raw || typeof raw !== "object") throw new OrderingBotError("order is required for order events");
  const o = raw as Record<string, unknown>;
  const number = text(o.number, 40);
  if (!number) throw new OrderingBotError("order.number is required");
  const crmStatus = text(o.crm_status, 20);
  if (crmStatus && !ORDER_STATUSES.has(crmStatus)) {
    throw new OrderingBotError(`order.crm_status must be one of ${[...ORDER_STATUSES].join(", ")}`);
  }
  const rawItems = Array.isArray(o.items) ? o.items : [];
  if (rawItems.length > MAX_ITEMS) throw new OrderingBotError(`at most ${MAX_ITEMS} items`);
  const items: BotItem[] = rawItems.map((it, index) => {
    const item = (it ?? {}) as Record<string, unknown>;
    const name = text(item.name, 200);
    const quantity = Number(item.quantity);
    if (!name || !Number.isInteger(quantity) || quantity < 1 || quantity > 10000) {
      throw new OrderingBotError(`order.items[${index}] needs a name and a whole quantity`);
    }
    return {
      name,
      sku: text(item.sku, 100),
      unit: text(item.unit, 60),
      quantity,
      rate: money(item.rate, `order.items[${index}].rate`),
      amount: money(item.amount, `order.items[${index}].amount`),
    };
  });
  return {
    number,
    stage: text(o.stage, 40),
    crmStatus: (crmStatus as OrderStatus | null) ?? null,
    items,
    subtotal: money(o.subtotal, "order.subtotal"),
    deliveryCharge: money(o.delivery_charge, "order.delivery_charge"),
    discount: money(o.discount, "order.discount"),
    discountCode: text(o.discount_code, 60),
    tax: money(o.tax, "order.tax"),
    total: money(o.total, "order.total"),
    deliveryDate: dateOrNull(o.delivery_date),
    slotLabel: text(o.slot_label, 120),
    address: text(o.address, 500),
    pincode: text(o.pincode, 20),
    latitude: numberOrNull(o.latitude),
    longitude: numberOrNull(o.longitude),
    kitchen: text(o.kitchen, 160),
    paymentReference: text(o.payment_reference, 120),
    instructions: text(o.instructions, 500),
    refundAmount: o.refund_amount === null || o.refund_amount === undefined ? null : money(o.refund_amount, "order.refund_amount"),
    feedback: text(o.feedback, 40),
  };
}

export function parseEvent(body: unknown): BotEvent {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new OrderingBotError("Expected a JSON object");
  const b = body as Record<string, unknown>;
  const event = text(b.event, 40);
  if (!event || !EVENTS.has(event)) throw new OrderingBotError(`Unknown event: ${event ?? "(none)"}`);
  const id = text(b.event_id, 64);
  if (!id) throw new OrderingBotError("event_id is required");

  const c = (b.customer ?? {}) as Record<string, unknown>;
  const customerId = text(c.id, 64);
  const phone = text(c.phone, 32);
  if (!customerId || !phone || lastTenDigits(phone).length < 10) {
    throw new OrderingBotError("customer.id and a customer.phone with at least 10 digits are required");
  }
  const customer: BotCustomer = {
    id: customerId,
    phone,
    name: text(c.name, 160),
    email: text(c.email, 255),
    address: text(c.address, 500),
    pincode: text(c.pincode, 20),
  };

  let nudge: BotNudge | null = null;
  if (event === "nudge") {
    const n = (b.nudge ?? {}) as Record<string, unknown>;
    const template = text(n.template, 160);
    const kind = text(n.kind, 40);
    if (!template || !kind) throw new OrderingBotError("nudge.template and nudge.kind are required");
    nudge = {
      kind,
      attempt: Math.max(1, Math.min(1000, Number(n.attempt) || 1)),
      template,
      status: text(n.status, 20) ?? "sent",
      reply: text(n.reply, 40),
      bucket: text(n.bucket, 40),
      providerMessageId: text(n.provider_message_id, 120),
      summary: text(n.summary, 2000),
    };
  }

  return {
    id,
    event,
    occurredAt: dateOrNull(b.occurred_at) ?? new Date(),
    customer,
    stage: parseStage(b.stage),
    nudge,
    order: event.startsWith("order.") ? parseOrder(b.order) : null,
  };
}

// --- writing it ---------------------------------------------------------------------------
interface Setup {
  verticalId: string;
  botUserId: string;
}

/** The Order Now vertical and the system user, made once and found after. */
async function ensureSetup(tx: Tx): Promise<Setup> {
  const vertical = await tx.vertical.upsert({
    where: { key: VERTICAL_KEY },
    update: {},
    create: { key: VERTICAL_KEY, label: VERTICAL_LABEL, sortOrder: VERTICAL_SORT_ORDER },
    select: { id: true },
  });
  let user = await tx.user.findUnique({ where: { email: BOT_USER_EMAIL }, select: { id: true } });
  if (!user) {
    const unusable = await bcrypt.hash(crypto.randomBytes(48).toString("base64"), 10);
    user = await tx.user.create({
      data: { name: BOT_USER_NAME, email: BOT_USER_EMAIL, passwordHash: unusable, role: "EXECUTIVE", isActive: false },
      select: { id: true },
    });
  }
  return { verticalId: vertical.id, botUserId: user.id };
}

/** As leadService.generateLeadCode: LD-<year>-<next number>. */
async function nextLeadCode(tx: Tx): Promise<string> {
  const prefix = `LD-${new Date().getFullYear()}-`;
  const last = await tx.lead.findFirst({
    where: { code: { startsWith: prefix } },
    orderBy: { code: "desc" },
    select: { code: true },
  });
  const lastNumber = last ? parseInt(last.code.slice(prefix.length), 10) || 0 : 0;
  return `${prefix}${String(lastNumber + 1).padStart(4, "0")}`;
}

/** The customer's Order Now lead: by the bot's customer id; else an unclaimed
 * lead with the same number that is Order Now or has no vertical yet (the
 * Welcome lead the CRM's own WhatsApp webhook made: DST's lead design keeps
 * ONE lead and sets its vertical on the customer's choice, here Order Now);
 * else a new one. A lead in another vertical is never taken. */
async function leadFor(tx: Tx, setup: Setup, c: BotCustomer, at: Date, applied: string[]): Promise<Lead> {
  const phone = displayPhone(c.phone);
  let lead = await tx.lead.findUnique({
    where: { externalSource_externalId: { externalSource: EXTERNAL_SOURCE, externalId: c.id } },
  });
  if (!lead) {
    // An Order Now lead first, then the most recent Welcome lead with no vertical.
    const samePhone = { externalSource: null, phone: { endsWith: lastTenDigits(c.phone) } };
    const unclaimed =
      (await tx.lead.findFirst({ where: { ...samePhone, verticalId: setup.verticalId }, orderBy: { updatedAt: "desc" } })) ??
      (await tx.lead.findFirst({ where: { ...samePhone, verticalId: null }, orderBy: { updatedAt: "desc" } }));
    if (unclaimed) {
      lead = await tx.lead.update({
        where: { id: unclaimed.id },
        data: { externalSource: EXTERNAL_SOURCE, externalId: c.id, verticalId: setup.verticalId },
      });
      applied.push(unclaimed.verticalId ? "lead linked" : "lead linked (vertical set to Order Now)");
    }
  }
  if (!lead) {
    lead = await tx.lead.create({
      data: {
        code: await nextLeadCode(tx),
        name: c.name || phone,
        phone,
        email: c.email,
        location: c.address,
        verticalId: setup.verticalId,
        source: LEAD_SOURCE,
        externalSource: EXTERNAL_SOURCE,
        externalId: c.id,
        stage: "NEW",
        lastContactAt: at,
        notes: "From the WhatsApp ordering bot (Hungry Now? Order Food).",
        createdAt: at,
        stageHistory: { create: { stageType: "CRM_STAGE", toStage: "NEW", changedAt: at } },
      },
    });
    applied.push("lead created");
    return lead;
  }
  // Fill in what is missing; never overwrite what the team typed.
  const data: Prisma.LeadUpdateInput = {};
  if (c.name && c.name !== lead.name && looksLikePhone(lead.name)) data.name = c.name;
  if (c.email && !lead.email) data.email = c.email;
  if (c.address && !lead.location) data.location = c.address;
  if (Object.keys(data).length) {
    lead = await tx.lead.update({ where: { id: lead.id }, data });
    applied.push("lead details");
  }
  return lead;
}

/** Pipeline stage and CRM stage, recorded as the CRM's own code records them. */
async function applyStage(tx: Tx, lead: Lead, stage: BotStage | null, at: Date, applied: string[]): Promise<Lead> {
  const data: Prisma.LeadUpdateInput = {};
  const history: Prisma.LeadStageHistoryCreateWithoutLeadInput[] = [];
  if (stage?.pipeline && stage.pipeline !== lead.gallaboxStageKey) {
    const target = await tx.pipelineStage.findUnique({ where: { key: stage.pipeline } });
    if (!target) throw new OrderingBotError(`Unknown pipeline stage: ${stage.pipeline}`, 422);
    data.gallaboxStage = { connect: { key: target.key } };
    data.pipelineStepIndex = target.sortOrder;
    history.push({ stageType: "GALLABOX_TEMPLATE", fromStage: lead.gallaboxStageKey, toStage: target.key, changedAt: at });
    applied.push(`pipeline ${lead.gallaboxStageKey ?? "-"} -> ${target.key}`);
  }
  // A lead the team (or a paid order) marked WON stays WON.
  if (stage?.crm && stage.crm !== lead.stage && lead.stage !== "WON") {
    data.stage = stage.crm;
    history.push({ stageType: "CRM_STAGE", fromStage: lead.stage, toStage: stage.crm, changedAt: at });
    applied.push(`stage ${lead.stage} -> ${stage.crm}`);
  }
  if (!lead.lastContactAt || lead.lastContactAt < at) data.lastContactAt = at;
  if (history.length) data.stageHistory = { create: history };
  if (!Object.keys(data).length) return lead;
  return tx.lead.update({ where: { id: lead.id }, data });
}

/** One activity per event and kind: a replayed event finds its own and stops. */
async function activity(
  tx: Tx, ev: BotEvent, setup: Setup, leadId: string, type: ActivityType,
  title: string, description: string | null, applied: string[], customerId?: string | null, status?: string,
): Promise<void> {
  const marker = `Ref: ${ev.id}/${type}`;
  const seen = await tx.crmActivity.findFirst({
    where: { leadId, type, description: { endsWith: marker } },
    select: { id: true },
  });
  if (seen) return;
  await tx.crmActivity.create({
    data: {
      leadId,
      customerId: customerId ?? null,
      type,
      title: title.slice(0, 200),
      description: [description, marker].filter(Boolean).join("\n\n"),
      occurredAt: ev.occurredAt,
      createdByUserId: setup.botUserId,
      status: status ?? null,
    },
  });
  applied.push(`activity ${type}`);
}

/** The paying customer as a CRM Customer (contact), with address and vertical. */
async function contactFor(tx: Tx, setup: Setup, ev: BotEvent, applied: string[]): Promise<string> {
  const c = ev.customer;
  const o = ev.order;
  const address = o?.address ?? c.address;
  const pincode = o?.pincode ?? c.pincode;
  let customer = await tx.customer.findFirst({
    where: { phone: { endsWith: lastTenDigits(c.phone) } },
    orderBy: { updatedAt: "desc" },
  });
  if (!customer) {
    customer = await tx.customer.create({
      data: {
        name: c.name || displayPhone(c.phone),
        phone: displayPhone(c.phone),
        email: c.email,
        location: address,
        pincode,
        customerSince: ev.occurredAt,
        deliveryInstructions: o?.instructions ?? null,
      },
    });
    applied.push("customer created");
  } else {
    const data: Prisma.CustomerUpdateInput = {};
    if (c.name && looksLikePhone(customer.name)) data.name = c.name;
    if (c.email && !customer.email) data.email = c.email;
    if (address && !customer.location) data.location = address;
    if (pincode && !customer.pincode) data.pincode = pincode;
    if (!customer.customerSince) data.customerSince = ev.occurredAt;
    if (Object.keys(data).length) {
      await tx.customer.update({ where: { id: customer.id }, data });
      applied.push("customer details");
    }
  }
  if (address) {
    const known = await tx.customerAddress.findFirst({ where: { customerId: customer.id, line: address }, select: { id: true } });
    if (!known) {
      const primaries = await tx.customerAddress.count({ where: { customerId: customer.id, isPrimary: true } });
      await tx.customerAddress.create({ data: { customerId: customer.id, line: address, isPrimary: primaries === 0 } });
      applied.push("address added");
    }
  }
  await tx.customerVertical.upsert({
    where: { customerId_verticalId: { customerId: customer.id, verticalId: setup.verticalId } },
    update: {},
    create: { customerId: customer.id, verticalId: setup.verticalId },
  });
  return customer.id;
}

function rupees(amount: string): string {
  return `₹${amount}`;
}

function itemLines(o: BotOrder): string {
  return o.items.map((i) => `${i.quantity} × ${i.name} - ${rupees(i.amount)}`).join("\n");
}

/** The Order with its line items, made once (the order number is unique). */
async function orderFor(tx: Tx, setup: Setup, customerId: string, ev: BotEvent, applied: string[]) {
  const o = ev.order as BotOrder;
  const existing = await tx.order.findUnique({ where: { orderNumber: o.number } });
  if (existing) return existing;
  const remarks = [
    "WhatsApp ordering bot (Hungry Now? Order Food)",
    o.kitchen ? `Kitchen: ${o.kitchen}` : null,
    Number(o.tax) > 0 ? `GST: ${rupees(o.tax)}` : null,
    o.paymentReference ? `Razorpay payment: ${o.paymentReference}` : null,
    o.instructions ? `Delivery note: ${o.instructions}` : null,
  ].filter(Boolean).join(" · ");
  const order = await tx.order.create({
    data: {
      orderNumber: o.number,
      customerId,
      verticalId: setup.verticalId,
      deliveryAddress: o.address,
      deliveryDate: o.deliveryDate,
      deliveryTimeSlot: o.slotLabel,
      deliveryPincode: o.pincode,
      deliveryLatitude: o.latitude,
      deliveryLongitude: o.longitude,
      status: o.crmStatus ?? "CONFIRMED",
      paymentMode: "PREPAID",
      discountCode: o.discountCode,
      discountAmount: o.discount,
      deliveryCharge: o.deliveryCharge,
      deliveryChargeReason: Number(o.deliveryCharge) > 0 ? "Delivered by DST India's own staff" : null,
      amountToCollect: "0.00",
      subtotal: o.subtotal,
      totalAmount: o.total,
      remarks,
      placedByUserId: setup.botUserId,
      createdAt: ev.occurredAt,
      lineItems: {
        create: o.items.map((i) => ({ name: i.name, sku: i.sku, unit: i.unit, quantity: i.quantity, rate: i.rate, amount: i.amount })),
      },
    },
  });
  applied.push(`order ${o.number} created (${o.items.length} items)`);
  return order;
}

async function apply(tx: Tx, ev: BotEvent): Promise<EventResult> {
  const applied: string[] = [];
  const setup = await ensureSetup(tx);
  let lead = await leadFor(tx, setup, ev.customer, ev.occurredAt, applied);
  const result: EventResult = { leadId: lead.id, leadCode: lead.code, applied };

  if (ev.event === "lead.stage") {
    lead = await applyStage(tx, lead, ev.stage, ev.occurredAt, applied);
    if (ev.stage?.note) {
      await activity(tx, ev, setup, lead.id, "NOTE", `Ordering bot: ${ev.stage.ours ?? "update"}`, ev.stage.note, applied, lead.customerId);
    }
    return result;
  }

  if (ev.event === "nudge") {
    const n = ev.nudge as BotNudge;
    lead = await applyStage(tx, lead, ev.stage, ev.occurredAt, applied);
    if (n.reply) {
      await activity(tx, ev, setup, lead.id, "WHATSAPP", `Nudge reply: ${n.reply}`,
        [`On ${n.template}.`, n.summary].filter(Boolean).join("\n"), applied, lead.customerId, n.reply);
      return result;
    }
    const sent = n.status === "sent";
    await activity(tx, ev, setup, lead.id, "WHATSAPP",
      `${sent ? "Nudge sent" : "Nudge failed"}: ${n.template}`,
      [`${n.kind === "stage" ? "Stage message" : `${n.bucket ?? n.kind} follow-up`} ${n.attempt}.`, n.summary]
        .filter(Boolean).join("\n"),
      applied, lead.customerId, sent ? "Sent" : "Failed");
    if (sent) {
      const logged = await tx.messageLog.findFirst({
        where: { leadId: lead.id, rawPayload: { path: ["eventId"], equals: ev.id } },
        select: { id: true },
      });
      if (!logged) {
        const template = ev.stage?.pipeline
          ? await tx.whatsAppTemplate.findFirst({
              where: { stageKey: ev.stage.pipeline, verticalId: null, active: true },
              select: { id: true },
            })
          : null;
        const idFree = n.providerMessageId
          ? !(await tx.messageLog.findUnique({ where: { gallaboxMsgId: n.providerMessageId }, select: { id: true } }))
          : false;
        await tx.messageLog.create({
          data: {
            leadId: lead.id,
            templateId: template?.id ?? null,
            gallaboxMsgId: idFree ? n.providerMessageId : null,
            direction: "outbound",
            deliveryStatus: "Sent",
            rawPayload: {
              source: "ordering-bot", eventId: ev.id, template: n.template, kind: n.kind,
              attempt: n.attempt, bucket: n.bucket, summary: n.summary,
            },
            sentAt: ev.occurredAt,
          },
        });
        applied.push("message logged");
      }
    }
    return result;
  }

  // Order events. Every one carries the whole order, so the order is made
  // here if an earlier event never arrived.
  const o = ev.order as BotOrder;
  const customerId = await contactFor(tx, setup, ev, applied);
  if (lead.customerId !== customerId) {
    lead = await tx.lead.update({ where: { id: lead.id }, data: { customer: { connect: { id: customerId } } } });
    applied.push("lead linked to customer");
  }
  const order = await orderFor(tx, setup, customerId, ev, applied);
  result.customerId = customerId;
  result.orderId = order.id;
  lead = await applyStage(tx, lead, ev.stage, ev.occurredAt, applied);

  if (ev.event === "order.paid") {
    await activity(tx, ev, setup, lead.id, "ORDER", `Order ${o.number} placed - ${rupees(o.total)}`,
      [itemLines(o), o.slotLabel ? `Delivery: ${o.slotLabel}` : null, o.address ? `To: ${o.address}` : null,
        o.kitchen ? `Kitchen: ${o.kitchen}` : null].filter(Boolean).join("\n"),
      applied, customerId, o.stage ?? "Paid");
    await activity(tx, ev, setup, lead.id, "PAYMENT", `Paid ${rupees(o.total)} online`,
      o.paymentReference ? `Razorpay payment ${o.paymentReference}` : null, applied, customerId, "Paid");
    return result;
  }

  if (ev.event === "order.status") {
    if (o.crmStatus && o.crmStatus !== order.status) {
      await tx.order.update({ where: { id: order.id }, data: { status: o.crmStatus } });
      applied.push(`order status ${order.status} -> ${o.crmStatus}`);
    }
    await activity(tx, ev, setup, lead.id, "ORDER", `Order ${o.number}: ${o.stage ?? o.crmStatus ?? "updated"}`,
      null, applied, customerId, o.stage ?? undefined);
    if (o.refundAmount && Number(o.refundAmount) > 0) {
      await activity(tx, ev, setup, lead.id, "PAYMENT", `Refunded ${rupees(o.refundAmount)}`,
        `Order ${o.number}, through Razorpay`, applied, customerId, "Refunded");
    }
    return result;
  }

  // order.feedback
  await activity(tx, ev, setup, lead.id, "NOTE", `Feedback on ${o.number}: ${o.feedback ?? "-"}`,
    null, applied, customerId, o.feedback ?? undefined);
  return result;
}

function isUniqueViolation(error: unknown): boolean {
  return error instanceof Prisma.PrismaClientKnownRequestError && error.code === "P2002";
}

/** Apply one event in one transaction (rolled back when `dryRun`). A clash on
 * a new lead code - another sync made a lead at the same moment - is retried. */
export async function handleOrderingBotEvent(body: unknown, options: { dryRun?: boolean } = {}) {
  const ev = parseEvent(body);
  for (let attempt = 1; ; attempt++) {
    try {
      const result = await prisma.$transaction(async (tx: Tx) => {
        const applied = await apply(tx, ev);
        if (options.dryRun) throw new DryRunRollback(applied);
        return applied;
      }, { maxWait: 10_000, timeout: 30_000 });
      return { ...result, event: ev.event, eventId: ev.id, dryRun: false };
    } catch (error) {
      if (error instanceof DryRunRollback) return { ...error.result, event: ev.event, eventId: ev.id, dryRun: true };
      if (isUniqueViolation(error) && attempt < 3) continue;
      throw error;
    }
  }
}

/** What the bot's "test connection" reads: is the CRM side ready. */
export async function orderingBotHealth() {
  const [vertical, user, stages] = await Promise.all([
    prisma.vertical.findUnique({ where: { key: VERTICAL_KEY }, select: { id: true } }),
    prisma.user.findUnique({ where: { email: BOT_USER_EMAIL }, select: { isActive: true } }),
    prisma.pipelineStage.findMany({ select: { key: true }, orderBy: { sortOrder: "asc" } }),
  ]);
  return {
    success: true,
    integration: "ordering-bot",
    version: INTEGRATION_VERSION,
    vertical: vertical ? VERTICAL_KEY : null,
    systemUser: user ? (user.isActive ? "active" : "inactive (cannot sign in)") : null,
    pipelineStages: stages.map((s: { key: string }) => s.key),
  };
}
