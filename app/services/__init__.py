"""Business services.

Submodules are imported here so `from app.services import orders` resolves for
static analysis as well as at runtime.
"""

from app.services import (
    connection_tests,
    crm_sync,
    customers,
    kitchen,
    menu,
    menu_import,
    orders,
    payments,
    pricing,
    settings_store,
    slots,
)

__all__ = [
    "connection_tests",
    "crm_sync",
    "customers",
    "kitchen",
    "menu",
    "menu_import",
    "orders",
    "payments",
    "pricing",
    "settings_store",
    "slots",
]
