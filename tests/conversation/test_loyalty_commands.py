"""Reward commands do not interfere with free-text answers or live payments."""
import pytest
from app.db.models import ConversationStep
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import cart
from app.services.conversation.context import FlowContext
from app.services.conversation.engine import _handle_global_intent
from app.integrations.gallabox.sender import use_sender
from tests.services.test_loyalty import configure, grant

async def context(session, customer, step, text):
    conversation, _ = await cart.for_customer(session, customer)
    conversation.step = step
    return FlowContext(session=session,customer=customer,conversation=conversation,
                       event=InboundEvent(whatsapp_number=customer.whatsapp_number,kind=InboundKind.TEXT,text=text))

class Sender:
    def __init__(self): self.sent=[]
    async def send_text(self,to,body,**kwargs): self.sent.append(body)

async def test_points_command_and_redemption_request(session,customer):
    await configure(session)
    await grant(session,customer)
    sender=Sender()
    with use_sender(sender):
        ctx=await context(session,customer,ConversationStep.CART_REVIEW,"points")
        assert await _handle_global_intent(ctx)
        assert "1000 available points" in sender.sent[-1]
        ctx=await context(session,customer,ConversationStep.CART_REVIEW,"redeem 200")
        assert await _handle_global_intent(ctx)
        assert ctx.get("loyalty_points")==200
        ctx=await context(session,customer,ConversationStep.CART_REVIEW,"redeem 0")
        assert await _handle_global_intent(ctx)
        assert ctx.get("loyalty_points")==0

async def test_points_is_not_an_intent_when_entering_address(session,customer):
    ctx=await context(session,customer,ConversationStep.AWAIT_ADDRESS,"points")
    assert not await _handle_global_intent(ctx)

async def test_cannot_change_redemption_on_live_payment_link(session,customer):
    sender=Sender()
    with use_sender(sender):
        ctx=await context(session,customer,ConversationStep.AWAIT_PAYMENT,"redeem 200")
        assert await _handle_global_intent(ctx)
    assert "Change menu" in sender.sent[-1]
    assert not ctx.get("loyalty_points")
