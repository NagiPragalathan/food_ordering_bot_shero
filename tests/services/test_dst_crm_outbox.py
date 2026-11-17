"""DST India's own CRM (services/dst_crm.py): what is queued for each thing
that happens to a customer, and how the outbox sends it - in order, retrying,
never twice. The CRM itself is never called: post_event is replaced."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import tenants
from app.core.config import settings_for
from app.db.models import (CrmOutbox, Customer, LeadStage, NudgeBucket, NudgeSend, Order,
                           OrderStage, Outlet)
from app.services import crm_sync, dst_crm
from app.workers import jobs

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def dst(monkeypatch):
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "crm_api_url", "https://crm.example.test/")
    monkeypatch.setattr(own, "crm_api_token", "test-token")
    with tenants.use(tenants.DST):
        yield own


@pytest.fixture
def zoho_off(monkeypatch):
    """Zoho is never called (DST India has none connected)."""
    from app.integrations.zoho import crm as zoho_crm

    def refuse(*_, **__):
        raise AssertionError("Zoho was called")

    for name in ("find_by_phone", "create_lead", "update_record", "convert_lead", "create_order"):
        monkeypatch.setattr(zoho_crm, name, refuse)


@pytest.fixture
async def indian(session) -> Customer:
    record = Customer(whatsapp_number="919876543210", name="Priya Raman", email="priya@example.com",
                      address_line1="12 Gandhi Street, T. Nagar", apartment_unit="Flat 3B",
                      postal_code="600017")
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def paid_order(session, indian) -> Order:
    kitchen = Outlet(code="TNAGAR", name="T. Nagar kitchen", address_line1="1 Usman Road",
                     city="Chennai", state="TN", postal_code="600017",
                     country="IN", timezone="Asia/Kolkata",
                     latitude=13.04, longitude=80.23)
    session.add(kitchen)
    await session.flush()
    order = Order(
        order_number="DST-261117-K4T9P", customer_id=indian.id, outlet_id=kitchen.id,
        items=[{"retailer_id": "ERP-101", "name": "Tomato Rasam", "quantity": 2, "unit_price": "120.00"},
               {"retailer_id": "ERP-202", "name": "Carrot Beans Poriyal", "quantity": 1, "unit_price": "160"}],
        dish_total=Decimal("400.00"), delivery_fee=Decimal("40.00"), tax=Decimal("20.00"),
        discount=Decimal("10.00"), loyalty_discount=Decimal("5.00"), promo_code="WELCOME10",
        total=Decimal("445.00"), currency="INR",
        delivery_address="12 Gandhi Street, T. Nagar", apartment_unit="Flat 3B", postal_code="600017",
        delivery_latitude=Decimal("13.041000"), delivery_longitude=Decimal("80.233000"),
        delivery_instructions="Ring twice",
        slot_starts_at=datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc),
        slot_label="Wed 19 Nov, 12:00 PM - 1:00 PM",
        stage=OrderStage.PAID_SLOT_BOOKED, stripe_payment_intent_id="pay_ABC123",
    )
    session.add(order)
    await session.flush()
    return order


async def outbox(session) -> list[CrmOutbox]:
    return list((await session.execute(select(CrmOutbox).order_by(CrmOutbox.queued_at))).scalars())


# --- what is queued ----------------------------------------------------------------------
async def test_each_ordering_step_moves_the_lead_along_the_pipeline(session, indian, dst, zoho_off):
    assert await dst_crm.record_new_enquiry(indian) == "welcome"    # their first message
    await dst_crm.record_new_enquiry(indian)                        # "hi" again: not queued twice
    for stage in (LeadStage.DETAILS_CAPTURED, LeadStage.CART_CREATED, LeadStage.PAYMENT_LINK_SENT):
        await crm_sync.advance_stage(indian, stage)
    rows = await outbox(session)
    assert [(r.event, r.payload["stage"]["pipeline"], r.payload["stage"]["crm"]) for r in rows] == [
        ("lead.stage", "welcome", "NEW"),
        ("lead.stage", "requirementCapture", "CONTACTED"),
        ("lead.stage", "requirementCapture", "IN_DISCUSSION"),
        ("lead.stage", "requirementCapture", "IN_DISCUSSION"),
    ]
    first = rows[0].payload
    assert first["event_id"] == str(rows[0].id)
    assert first["customer"] == {"id": str(indian.id), "phone": "919876543210", "name": "Priya Raman",
                                 "email": "priya@example.com", "address": "12 Gandhi Street, T. Nagar, Flat 3B",
                                 "pincode": "600017"}
    assert all(r.status == "pending" for r in rows)


async def test_an_address_out_of_reach_is_a_note_not_a_stage(session, indian, dst, zoho_off):
    await crm_sync.advance_stage(indian, LeadStage.NOT_SERVICEABLE)
    (row,) = await outbox(session)
    assert row.payload["stage"]["pipeline"] is None
    assert row.payload["stage"]["note"] == "No kitchen delivers to 12 Gandhi Street, T. Nagar, 600017"


async def test_a_paid_order_goes_with_its_dishes_contact_and_kitchen(session, indian, paid_order,
                                                                    dst, zoho_off):
    ctx = crm_sync.OrderContext(outlet=await session.get(Outlet, paid_order.outlet_id))
    await crm_sync.convert_and_record_order(indian, paid_order, ctx)
    await crm_sync.convert_and_record_order(indian, paid_order, ctx)   # a replayed webhook
    (row,) = await outbox(session)
    assert row.event == "order.paid" and row.dedupe_key == "order.paid:DST-261117-K4T9P"
    assert row.payload["stage"] == {"ours": "Converted", "pipeline": "orderConfirmed", "crm": "WON"}
    order = row.payload["order"]
    assert order["items"] == [
        {"name": "Tomato Rasam", "sku": "ERP-101", "quantity": 2, "rate": "120.00", "amount": "240.00"},
        {"name": "Carrot Beans Poriyal", "sku": "ERP-202", "quantity": 1, "rate": "160.00", "amount": "160.00"},
    ]
    assert (order["subtotal"], order["delivery_charge"], order["discount"], order["tax"], order["total"]) == \
        ("400.00", "40.00", "15.00", "20.00", "445.00")
    assert order["discount_code"] == "WELCOME10" and order["crm_status"] == "CONFIRMED"
    assert order["kitchen"] == "T. Nagar kitchen" and order["payment_reference"] == "pay_ABC123"
    assert order["address"] == "12 Gandhi Street, T. Nagar, Flat 3B" and order["pincode"] == "600017"
    assert order["delivery_date"] == "2026-11-19T06:30:00+00:00"
    assert order["slot_label"] == "Wed 19 Nov, 12:00 PM - 1:00 PM"
    assert (order["latitude"], order["longitude"]) == (13.041, 80.233)
    assert order["instructions"] == "Ring twice"


async def test_each_order_step_updates_the_order_once(session, indian, paid_order, dst, zoho_off):
    await crm_sync.push_order_stage(paid_order, OrderStage.SENT_TO_KITCHEN)
    await crm_sync.push_order_stage(paid_order, OrderStage.OUT_FOR_DELIVERY)
    await crm_sync.push_order_stage(paid_order, OrderStage.OUT_FOR_DELIVERY)   # pressed twice
    await crm_sync.push_order_stage(paid_order, OrderStage.DELIVERED)
    paid_order.refund_amount = Decimal("445.00")
    await crm_sync.push_order_stage(paid_order, OrderStage.REFUNDED)
    rows = await outbox(session)
    assert [(r.payload["order"]["stage"], r.payload["order"]["crm_status"],
             (r.payload["stage"] or {}).get("pipeline")) for r in rows] == [
        ("Sent to Kitchen", "CONFIRMED", "orderConfirmed"),
        ("Out for Delivery", "CONFIRMED", "deliveryStatus"),
        ("Delivered", "DELIVERED", "deliveryStatus"),
        ("Refunded", "CANCELLED", None),
    ]
    assert rows[-1].payload["order"]["refund_amount"] == "445.00"
    assert all(r.payload["order"]["items"] for r in rows)   # each can make the order on its own


async def test_feedback_is_a_note_on_the_order(session, indian, paid_order, dst):
    await dst_crm.record_feedback(indian, paid_order, "Great")
    (row,) = await outbox(session)
    assert row.event == "order.feedback" and row.payload["order"]["feedback"] == "Great"


@pytest.mark.parametrize(("kind", "attempt", "template", "reply", "expected"), [
    (NudgeBucket.NO_RESPONSE, 1, "dst_nudge_cart", None, "rnr1"),
    (NudgeBucket.NO_RESPONSE, 2, "dst_nudge_cart", None, "rnr2"),
    (NudgeBucket.NO_RESPONSE, 3, "dst_nudge_cart", None, "rnrFinal"),
    ("stage", 1, "dst_nudge_cart", None, None),          # a stage message: stays in its stage
    (NudgeBucket.LATER, 1, "dst_nudge_later", None, None),
    ("stage", 1, "dst_rnr_2", None, "rnr2"),             # DST's own follow-up templates
    (NudgeBucket.LATER, 1, "dst_win_back", None, "winback"),
    (NudgeBucket.NO_RESPONSE, 1, "dst_rnr_final", None, "rnrFinal"),
    ("stage", 1, "dst_nudge_cart", "No", "lostFeedback"),
    ("stage", 1, "dst_nudge_cart", "STOP", "lostFeedback"),
    ("stage", 1, "dst_nudge_cart", "Later", None),
    ("stage", 1, "dst_nudge_cart", "Order Food", None),
])
def test_where_a_nudge_puts_the_lead(kind, attempt, template, reply, expected):
    send = NudgeSend(kind=kind, attempt=attempt, template=template, status="sent")
    assert dst_crm.stage_for_nudge(send, reply) == expected


async def test_a_nudge_and_a_reply_are_queued(session, indian, dst):
    send = NudgeSend(customer_id=indian.id, kind=NudgeBucket.NO_RESPONSE, stage="Cart Created",
                     attempt=1, template="dst_nudge_cart", status="sent", sent_at=NOW,
                     provider_message_id="gb-1")
    session.add(send)
    await dst_crm.record_nudge(indian, send, bucket=NudgeBucket.NO_RESPONSE, summary="Hi Priya, your cart...")
    await dst_crm.record_nudge(indian, send, bucket=NudgeBucket.NO, reply="No")
    first, second = await outbox(session)
    assert first.payload["stage"] == {"pipeline": "rnr1", "crm": None}
    assert first.payload["nudge"]["provider_message_id"] == "gb-1"
    assert first.payload["nudge"]["summary"] == "Hi Priya, your cart..."
    assert second.payload["stage"] == {"pipeline": "lostFeedback", "crm": "LOST"}
    assert second.payload["nudge"]["reply"] == "No"


async def test_a_failed_send_is_logged_without_moving_the_lead(session, indian, dst):
    send = NudgeSend(customer_id=indian.id, kind=NudgeBucket.NO_RESPONSE, attempt=1,
                     template="dst_nudge_cart", status="failed", error="not approved", sent_at=NOW)
    session.add(send)
    await dst_crm.record_nudge(indian, send)
    (row,) = await outbox(session)
    assert row.payload["stage"] is None and row.payload["nudge"]["status"] == "failed"
    assert row.payload["nudge"]["summary"] == "not approved"


async def test_nothing_is_queued_for_shero_us_or_without_the_crm(session, customer, indian, monkeypatch):
    # Shero US, even with the DST CRM's settings filled in.
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "crm_api_url", "https://crm.example.test")
    monkeypatch.setattr(own, "crm_api_token", "test-token")
    assert await dst_crm.record_lead_stage(customer, LeadStage.NEW_ENQUIRY) is None
    # DST India with no address/key.
    monkeypatch.setattr(own, "crm_api_token", "")
    with tenants.use(tenants.DST):
        assert await dst_crm.record_lead_stage(indian, LeadStage.NEW_ENQUIRY) == "welcome"
        assert await jobs.send_crm_events() == 0
    assert await outbox(session) == []


# --- sending ---------------------------------------------------------------------------------
@pytest.fixture
def crm(monkeypatch):
    """The CRM, answering from a script: each call takes the next answer."""
    calls: list[dict] = []
    answers: list = []

    async def post(payload):
        calls.append(payload)
        answer = answers.pop(0) if answers else {"applied": ["ok"]}
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dst_crm, "post_event", post)
    return calls, answers


async def test_waiting_events_go_in_order_and_are_marked_sent(session, indian, dst, crm, zoho_off):
    calls, answers = crm
    await dst_crm.record_new_enquiry(indian)
    await crm_sync.advance_stage(indian, LeadStage.CART_CREATED)
    answers.extend([{"applied": ["lead created", "pipeline - -> welcome"]}, {"applied": []}])
    counts = await dst_crm.flush(session, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert counts == {"sent": 2, "retry": 0, "failed": 0}
    assert [c["stage"]["pipeline"] for c in calls] == ["welcome", "requirementCapture"]
    first, second = await outbox(session)
    assert first.status == second.status == "sent" and first.result == "lead created; pipeline - -> welcome"
    assert await dst_crm.flush(session) == {"sent": 0, "retry": 0, "failed": 0}   # nothing twice


async def test_a_crm_outage_holds_that_customer_s_later_events(session, indian, customer, dst, crm,
                                                                zoho_off):
    calls, answers = crm
    other = Customer(whatsapp_number="919800000002", name="Kavya")
    session.add(other)
    await session.flush()
    await dst_crm.record_new_enquiry(indian)
    await crm_sync.advance_stage(indian, LeadStage.CART_CREATED)
    await dst_crm.record_new_enquiry(other)
    answers.append(dst_crm._Retry("CRM answered 502: Bad Gateway"))
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    counts = await dst_crm.flush(session, now=later)
    # Priya's first event failed, so her second waits; Kavya's goes.
    assert counts == {"sent": 1, "retry": 1, "failed": 0}
    assert [c["customer"]["name"] for c in calls] == ["Priya Raman", "Kavya"]
    first = (await outbox(session))[0]
    assert first.status == "pending" and first.attempts == 1
    assert first.last_error == "CRM answered 502: Bad Gateway"
    assert first.next_attempt_at.replace(tzinfo=timezone.utc) == later + timedelta(minutes=1)
    # A minute on, the CRM is back: both of Priya's go, in order.
    calls.clear()
    counts = await dst_crm.flush(session, now=later + timedelta(minutes=2))
    assert counts["sent"] == 2
    assert [c["stage"]["pipeline"] for c in calls] == ["welcome", "requirementCapture"]


async def test_an_event_the_crm_refuses_is_set_aside_and_can_be_sent_again(session, indian, dst, crm,
                                                                          zoho_off):
    calls, answers = crm
    await dst_crm.record_new_enquiry(indian)
    answers.append(dst_crm._Refused("CRM answered 422: Unknown pipeline stage: welcome"))
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert (await dst_crm.flush(session, now=later))["failed"] == 1
    (row,) = await outbox(session)
    assert row.status == "failed" and "422" in row.last_error
    state = await dst_crm.status(session)
    assert (state.failed, state.pending, state.connected) == (1, 0, True)
    assert await dst_crm.retry_failed(session) == 1
    assert (await dst_crm.flush(session, now=later + timedelta(seconds=5)))["sent"] == 1


async def test_after_a_week_of_failures_an_event_is_given_up(session, indian, dst, crm, zoho_off):
    _, answers = crm
    await dst_crm.record_new_enquiry(indian)
    answers.append(dst_crm._Retry("CRM not reachable"))
    counts = await dst_crm.flush(session, now=datetime.now(timezone.utc) + timedelta(days=8))
    assert counts["failed"] == 1 and (await outbox(session))[0].status == "failed"


async def test_post_event_sorts_answers_into_retry_and_refused(monkeypatch, dst):
    import httpx

    def answering(code: int, body: dict):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-ordering-bot-token"] == "test-token"
            assert str(request.url) == "https://crm.example.test/api/integrations/ordering-bot/events"
            return httpx.Response(code, json=body)
        return handler

    real = httpx.AsyncClient

    def client_with(handler):
        return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(dst_crm.httpx, "AsyncClient", client_with(answering(200, {"applied": ["x"]})))
    assert await dst_crm.post_event({"event": "lead.stage"}) == {"applied": ["x"]}
    for code, error in ((422, dst_crm._Refused), (400, dst_crm._Refused), (404, dst_crm._Retry),
                        (401, dst_crm._Retry), (503, dst_crm._Retry)):
        monkeypatch.setattr(dst_crm.httpx, "AsyncClient", client_with(answering(code, {"message": "m"})))
        with pytest.raises(error):
            await dst_crm.post_event({"event": "lead.stage"})


# --- the Settings page card ----------------------------------------------------------------
def test_the_settings_page_has_the_dst_crm_card_only_on_dst_india(session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_primary_session, get_session

    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "crm_api_url", "https://crm.example.test")
    monkeypatch.setattr(own, "crm_api_token", "test-token")
    checked = []

    async def health():
        checked.append(tenants.current().key)
        return {"ok": True, "pipelineStages": list(dst_crm.STAGES)}

    monkeypatch.setattr(dst_crm, "health", health)
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_primary_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    admin = TestClient(app, follow_redirects=False)

    assert "data-dst-crm-card" not in admin.get("/admin/settings").text
    page = admin.get("/dst/admin/settings").text
    card = page.split("data-dst-crm-card", 1)[1].split("</section>", 1)[0]
    assert "connected" in card and "https://crm.example.test" in card and "Waiting" in card

    sent = admin.post("/dst/admin/settings/dst-crm/check")
    assert sent.status_code == 303 and sent.headers["location"].endswith("/dst/admin/settings#dst-crm")
    assert checked == ["dst"]
    assert admin.post("/admin/settings/dst-crm/check").status_code == 303   # Shero US: refused
    assert checked == ["dst"]
