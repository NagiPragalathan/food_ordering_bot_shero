"""Which payment service takes the money for the business being served.

Shero US pays through Stripe Checkout, DST India through Razorpay Payment
Links (PAYMENT_PROVIDER, app/core/config.py). Both modules offer the same
functions with the same shapes - create_checkout_session, session_state,
expire_session, create_refund, mock_mode, mock_checkout_url,
MOCK_SESSION_PREFIX - so services/payments.py and the Pay Now redirect call
`current()` and never ask which one it is.
"""

from __future__ import annotations

from types import ModuleType

from app.core.config import settings
from app.integrations.razorpay_gw import checkout as razorpay_checkout
from app.integrations.stripe_gw import checkout as stripe_checkout

STRIPE, RAZORPAY = "stripe", "razorpay"
NAMES = {STRIPE: "Stripe", RAZORPAY: "Razorpay"}


def provider() -> str:
    return RAZORPAY if settings.payment_provider == RAZORPAY else STRIPE


def uses_razorpay() -> bool:
    return provider() == RAZORPAY


def current() -> ModuleType:
    """The checkout module of the business being served."""
    return razorpay_checkout if uses_razorpay() else stripe_checkout


def name() -> str:
    """"Stripe" or "Razorpay", for the pages and messages."""
    return NAMES[provider()]


def mock_mode() -> bool:
    """No keys: Pay Now opens the test payment page and nothing is charged."""
    return current().mock_mode()


def test_mode() -> bool:
    """Test keys: payments are real calls, but no real money moves."""
    if uses_razorpay():
        return razorpay_checkout.test_mode()
    key = settings.stripe_secret_key.strip()
    return key.startswith(("sk_test", "rk_test"))
