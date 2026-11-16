"""The businesses this one service runs: Shero US and DST India.

Each business is a complete, separate copy of the bot - its own WhatsApp
number, database, settings, menu, kitchens, orders, customers, nudges,
loyalty, payouts and admin pages - sharing only the code and the admin
logins. Nothing of one business is ever read or written while serving the
other:

    Shero US    /admin/...        data/shero.db        Stripe    Uber Direct
    DST India   /dst/admin/...    data/shero_dst.db    Razorpay  own delivery staff

Which business a piece of work belongs to is held in a ContextVar, set once
at the edge and never passed around:

  * a web request: by its path - everything under /dst is DST India
    (`TenantPathMiddleware`), so admin pages, ordering links, the Pay Now
    redirect and webhooks all carry their business in the URL;
  * a WhatsApp message: by the Gallabox channel it arrived on (one Gallabox
    account, one webhook, several numbers);
  * a scheduled job: run once per business (`each()`).

Everything below - `settings`, database sessions, token and template caches -
reads the current business from here, so the services themselves are the
same code for both and need no business argument.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Tenant:
    key: str            # "us" / "dst" - also the -x tenant=... of alembic
    name: str           # what the admin tabs say
    url_prefix: str     # "" or "/dst": where its pages and webhooks live
    env_prefix: str     # "" or "DST_": its own variables in .env

    @property
    def is_default(self) -> bool:
        return self.url_prefix == ""

    def path(self, path: str) -> str:
        """`path` (starting with /) inside this business: /admin -> /dst/admin."""
        return f"{self.url_prefix}{path}"


US = Tenant(key="us", name="Shero US", url_prefix="", env_prefix="")
DST = Tenant(key="dst", name="DST India", url_prefix="/dst", env_prefix="DST_")

ALL: tuple[Tenant, ...] = (US, DST)
DEFAULT = US
BY_KEY: dict[str, Tenant] = {t.key: t for t in ALL}

_current: ContextVar[Tenant] = ContextVar("tenant", default=DEFAULT)


def current() -> Tenant:
    """The business being served right now."""
    return _current.get()


def by_key(key: str | None) -> Tenant | None:
    return BY_KEY.get((key or "").strip().lower())


@contextmanager
def use(tenant: Tenant) -> Iterator[Tenant]:
    """Serve `tenant` inside this block (restored afterwards, nesting included)."""
    token = _current.set(tenant)
    try:
        yield tenant
    finally:
        _current.reset(token)


def enter(tenant: Tenant) -> None:
    """Serve `tenant` for the rest of this task (a request, a job)."""
    _current.set(tenant)


def for_path(path: str) -> Tenant:
    """The business a URL path belongs to: /dst and below is DST India."""
    for tenant in ALL:
        prefix = tenant.url_prefix
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return tenant
    return DEFAULT


_broken: dict[str, str] = {}


def enabled() -> list[Tenant]:
    """The businesses set up on this server: those with a database.

    Shero US always is. DST India is once it has a database (DST_DATABASE_URL,
    or the automatic file beside a SQLite Shero database).

    A business whose settings cannot be read (a mistyped DST_ variable, say
    DST_TAX_PERCENT=5%) is left out and logged, never raised: every Shero US
    path - each WhatsApp message, job, admin page and startup - asks this,
    and one business's bad setting must not take the other down."""
    from app.core.config import settings_for

    found = []
    for tenant in ALL:
        if tenant.is_default:
            found.append(tenant)
            continue
        if tenant.key in _broken:
            continue
        try:
            own = settings_for(tenant)
        except Exception as exc:  # noqa: BLE001 - see above
            _broken[tenant.key] = _why(exc, tenant.env_prefix)
            from app.core.logging import get_logger

            get_logger(__name__).error("business_settings_invalid", business=tenant.key,
                                       problem=_broken[tenant.key])
            continue
        if own.database_url.strip():
            found.append(tenant)
    return found


def _why(exc: Exception, env_prefix: str = "") -> str:
    """Which variables are wrong - by name only, never their values (a value
    can be a key): "invalid: DST_TAX_PERCENT"."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            names = sorted({".".join(str(p) for p in e.get("loc", ())) for e in errors()})
            return "invalid: " + ", ".join(env_prefix + n.upper() for n in names if n)
        except Exception:  # noqa: BLE001
            pass
    return type(exc).__name__


def each() -> Iterator[Tenant]:
    """Run the loop body once per business set up here, inside that business."""
    for tenant in enabled():
        with use(tenant):
            yield tenant


class PerTenant(Generic[T]):
    """One value per business, made on first use: a token cache, a template
    approval cache. A cache shared between the two would hand one business's
    Zoho token or template state to the other."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._values: dict[str, T] = {}

    def get(self) -> T:
        key = current().key
        if key not in self._values:
            self._values[key] = self._factory()
        return self._values[key]

    def set(self, value: T) -> None:
        self._values[current().key] = value

    def values(self) -> list[T]:
        return list(self._values.values())

    def clear(self) -> None:
        self._values.clear()


class TenantPathMiddleware:
    """Serve a request under /dst as DST India.

    The prefix moves into the ASGI root_path, the standard place for "this app
    is mounted under a path": routing then matches /dst/admin/menu as
    /admin/menu, and every url_for() builds links that keep /dst. So one set
    of routes and templates serves both businesses.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        root = scope.get("root_path", "")
        path = scope.get("path", "")
        inner = path[len(root):] if root and path.startswith(root) else path
        tenant = for_path(inner)
        if not tenant.is_default:
            scope = dict(scope)
            scope["root_path"] = root + tenant.url_prefix
        token = _current.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
