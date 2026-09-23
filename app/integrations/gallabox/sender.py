"""Which object outbound WhatsApp messages actually go to.

Everything that sends calls `current_sender()` rather than importing the
Gallabox client directly, so a caller can redirect one conversation without
disturbing anyone else's. The admin chat tester uses this to drive the real
bot while collecting the replies instead of delivering them.

It is a `ContextVar`, so the override is scoped to the current asyncio task:
two requests handled concurrently cannot see each other's sender. A global
would have swapped the sender for every customer mid-order.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.integrations.gallabox.client import gallabox

_override: ContextVar[Any | None] = ContextVar("gallabox_sender", default=None)


def current_sender() -> Any:
    """The client to send through - the real one unless overridden here."""
    return _override.get() or gallabox


@contextmanager
def use_sender(sender: Any):
    """Route sends inside this block to `sender`.

    Reset with the token rather than setting None back, so nesting restores
    the previous sender instead of jumping straight to the real client.
    """
    token = _override.set(sender)
    try:
        yield sender
    finally:
        _override.reset(token)
