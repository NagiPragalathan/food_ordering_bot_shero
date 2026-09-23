"""Application exception hierarchy.

Split into two families so callers can react correctly:

* `IntegrationError` - a third party failed. Usually retryable, and the
  customer should see a soft "please try again" rather than a dead end.
* `BotFlowError`     - the conversation cannot proceed as requested
  (not serviceable, item unavailable, no slots). These map onto the
  "If it fails" column of the spec and always have a customer-safe message.
"""

from __future__ import annotations


class SheroError(Exception):
    """Base for everything this application raises deliberately."""


class ConfigurationError(SheroError):
    """A required credential or setting is missing/invalid."""


class IntegrationError(SheroError):
    """An upstream API call failed."""

    def __init__(self, service: str, message: str, *, status_code: int | None = None,
                 payload: object | None = None) -> None:
        super().__init__(f"[{service}] {message}")
        self.service = service
        self.message = message
        self.status_code = status_code
        self.payload = payload

    @property
    def is_retryable(self) -> bool:
        if self.status_code is None:
            return True  # network-level failure
        return self.status_code == 429 or self.status_code >= 500


class BotFlowError(SheroError):
    """The flow cannot continue; carries a message safe to show the customer."""

    def __init__(self, message: str, *, customer_message: str | None = None) -> None:
        super().__init__(message)
        self.customer_message = customer_message or (
            "Sorry, something went wrong. Please try again in a moment."
        )


class NotServiceableError(BotFlowError):
    """No outlet is within delivery range (spec steps 6a / 10)."""


class ItemUnavailableError(BotFlowError):
    """A cart item is not available at the chosen outlet (spec steps 8 / 11)."""


class NoSlotsAvailableError(BotFlowError):
    """No delivery slot could be offered (spec step 13)."""


class PaymentError(SheroError):
    """Stripe checkout could not be created or reconciled."""
