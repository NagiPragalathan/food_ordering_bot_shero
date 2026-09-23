"""Admin authentication: Argon2 passwords, signed session cookies.

No third-party auth service and no JWT: this is a handful of staff logins on
one service, so a signed, expiring, http-only cookie is the right size of
solution.

The first account is created from ADMIN_BOOTSTRAP_EMAIL / _PASSWORD on
startup, once. Remove those variables afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.db.models import AdminUser

log = get_logger(__name__)

SESSION_COOKIE = "shero_admin"
SALT = "shero-admin-session"

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def password_problems(password: str) -> list[str]:
    """Minimum bar for an admin password. Returns a list of problems."""
    problems = []
    if len(password or "") < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if password and password.isdigit():
        problems.append("must not be only digits")
    if password and password.lower() in {"password12", "adminadmin", "changeme12"}:
        problems.append("is too common")
    return problems


def _serializer() -> URLSafeTimedSerializer:
    secret = settings.admin_session_secret
    if is_unset(secret):
        # Fail closed: an unsigned session cookie is an open front door.
        raise RuntimeError(
            "ADMIN_SESSION_SECRET is not set - the admin dashboard is disabled. "
            "Generate one with: openssl rand -hex 32"
        )
    return URLSafeTimedSerializer(secret, salt=SALT)


def issue_session(user: AdminUser) -> str:
    return _serializer().dumps({"id": str(user.id), "email": user.email})


def read_session(token: str | None) -> dict | None:
    """Decode a session cookie, or None if missing, tampered or expired."""
    if not token:
        return None
    try:
        return _serializer().loads(
            token, max_age=settings.admin_session_hours * 3600
        )
    except SignatureExpired:
        return None
    except (BadSignature, RuntimeError):
        return None


# --- users -------------------------------------------------------------------
async def authenticate(session: AsyncSession, email: str,
                       password: str) -> AdminUser | None:
    """Check an email/password pair. Returns the user, or None."""
    normalised = (email or "").strip().lower()
    if not normalised or not password:
        return None

    result = await session.execute(
        select(AdminUser).where(func.lower(AdminUser.email) == normalised)
    )
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        # Hash anyway so a missing account and a wrong password take the same
        # time, which stops the response time revealing which emails exist.
        _hasher.hash(password)
        return None

    if not verify_password(user.password_hash, password):
        log.info("admin_login_failed", email=normalised)
        return None

    user.last_login_at = datetime.now(timezone.utc)
    log.info("admin_login", email=normalised)
    return user


async def get_user(session: AsyncSession, user_id: str) -> AdminUser | None:
    import uuid

    try:
        return await session.get(AdminUser, uuid.UUID(user_id))
    except (ValueError, TypeError):
        return None


async def create_user(session: AsyncSession, email: str, password: str, *,
                      name: str | None = None) -> AdminUser:
    problems = password_problems(password)
    if problems:
        raise ValueError("Password " + "; ".join(problems))

    user = AdminUser(
        email=email.strip().lower(),
        name=name,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    log.info("admin_user_created", email=user.email)
    return user


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(AdminUser.id)))
    return int(result.scalar_one())


async def bootstrap_first_user(session: AsyncSession) -> AdminUser | None:
    """Create the first admin from env vars, if there are no users yet."""
    if await count_users(session) > 0:
        return None

    email = settings.admin_bootstrap_email
    password = settings.admin_bootstrap_password
    if is_unset(email) or is_unset(password):
        log.warning(
            "admin_bootstrap_skipped",
            reason="no admin users exist and ADMIN_BOOTSTRAP_EMAIL/PASSWORD are unset",
        )
        return None

    try:
        user = await create_user(session, email, password, name="Administrator")
    except ValueError as exc:
        log.error("admin_bootstrap_failed", error=str(exc))
        return None

    log.warning("admin_bootstrap_created", email=user.email,
                note="remove ADMIN_BOOTSTRAP_* from the environment now")
    return user
