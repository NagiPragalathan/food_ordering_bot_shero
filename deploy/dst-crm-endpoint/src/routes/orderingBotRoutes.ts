/**
 * /api/integrations/ordering-bot - the WhatsApp ordering bot's way in.
 *
 *   GET  /health              is the CRM side ready (vertical, system user, stages)
 *   POST /events              one event (see orderingBotService.ts)
 *   POST /events?dry_run=1    the same, rolled back: nothing is saved
 *
 * Every call needs the `x-ordering-bot-token` header to equal
 * ORDERING_BOT_TOKEN from the environment (timing-safe, like the Gallabox
 * webhook's token). With no ORDERING_BOT_TOKEN set, the integration is off.
 */
import crypto from "crypto";
import { Router } from "express";
import type { NextFunction, Request, Response } from "express";
import { handleOrderingBotEvent, orderingBotHealth, OrderingBotError } from "../integrations/orderingBot/orderingBotService";

const router = Router();

export function verifyOrderingBotToken(req: Request, res: Response, next: NextFunction) {
  const expected = (process.env.ORDERING_BOT_TOKEN ?? "").trim();
  if (!expected) {
    res.status(503).json({ success: false, message: "The ordering bot integration is not configured" });
    return;
  }
  const provided = req.headers["x-ordering-bot-token"];
  if (typeof provided !== "string") {
    res.status(401).json({ success: false, message: "Missing token" });
    return;
  }
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(provided, "utf8");
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    res.status(401).json({ success: false, message: "Invalid token" });
    return;
  }
  next();
}

router.get("/health", verifyOrderingBotToken, async (_req: Request, res: Response) => {
  try {
    res.json(await orderingBotHealth());
  } catch (error) {
    console.error("[ordering-bot] health failed:", error instanceof Error ? error.message : error);
    res.status(500).json({ success: false, message: "Internal error" });
  }
});

router.post("/events", verifyOrderingBotToken, async (req: Request, res: Response) => {
  const flag = String(req.query.dry_run ?? "");
  const dryRun = flag === "1" || flag === "true";
  try {
    const result = await handleOrderingBotEvent(req.body, { dryRun });
    res.json({ success: true, ...result });
  } catch (error) {
    if (error instanceof OrderingBotError) {
      res.status(error.status).json({ success: false, message: error.message });
      return;
    }
    console.error("[ordering-bot] event failed:", error instanceof Error ? error.message : error);
    res.status(500).json({ success: false, message: "Internal error" });
  }
});

export default router;
