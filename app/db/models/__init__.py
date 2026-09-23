"""SQLAlchemy models.

Imported as a package so Alembic's autogenerate sees every table via
`Base.metadata`.
"""

from app.db.base import Base
from app.db.models.conversation import Conversation, InboundMessage
from app.db.models.customer import Customer
from app.db.models.enums import (
    DELIVERY_DETAIL_SEQUENCE,
    ConversationStep,
    LeadStage,
    OrderStage,
    PaymentStatus,
    SlotHoldStatus,
)
from app.db.models.menu import Category, Cuisine, MenuItem
from app.db.models.order import Order
from app.db.models.outlet import Outlet
from app.db.models.setting import AdminUser, AppSetting
from app.db.models.slot import DeliverySlot, SlotHold

__all__ = [
    "AdminUser",
    "AppSetting",
    "Base",
    "Category",
    "Conversation",
    "ConversationStep",
    "Cuisine",
    "Customer",
    "DELIVERY_DETAIL_SEQUENCE",
    "DeliverySlot",
    "InboundMessage",
    "LeadStage",
    "MenuItem",
    "Order",
    "OrderStage",
    "Outlet",
    "PaymentStatus",
    "SlotHold",
    "SlotHoldStatus",
]
