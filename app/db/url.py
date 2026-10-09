"""The database URL in the form the async drivers accept.

Hosted Postgres (Neon, Vercel Postgres, Supabase) hands out URLs such as

    postgres://user:pass@host/db?sslmode=require&channel_binding=require

asyncpg speaks neither the `postgres://` scheme nor libpq's `sslmode` and
`channel_binding` options, so the URL is rewritten to

    postgresql+asyncpg://user:pass@host/db?ssl=require

MySQL (the database on the AWS server) is written plainly as

    mysql://user:pass@host:3306/db

and becomes `mysql+aiomysql://user:pass@host:3306/db?charset=utf8mb4`;
utf8mb4 so dish names and WhatsApp messages keep every character, emoji
included.

Anything already in driver form (postgresql+asyncpg://, mysql+aiomysql://,
sqlite+aiosqlite://) passes through unchanged.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# libpq-only options asyncpg would reject.
_LIBPQ_ONLY = {"sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey",
               "target_session_attrs", "gssencmode"}
_SSL_MODES = {"require", "verify-ca", "verify-full", "prefer", "allow"}


def driver_url(url: str) -> str:
    raw = (url or "").strip()
    scheme, rest = raw.split("://", 1) if "://" in raw else ("", raw)
    if scheme == "mysql":
        return _mysql_url(rest)
    if scheme not in ("postgres", "postgresql"):
        return raw

    parts = urlsplit(f"postgresql+asyncpg://{rest}")
    query = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = next((v for k, v in query if k == "sslmode"), "")
    kept = [(k, v) for k, v in query if k not in _LIBPQ_ONLY]
    if sslmode in _SSL_MODES and not any(k == "ssl" for k, _ in kept):
        kept.append(("ssl", "require"))
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _mysql_url(rest: str) -> str:
    parts = urlsplit(f"mysql+aiomysql://{rest}")
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "charset" for k, _ in query):
        query.append(("charset", "utf8mb4"))
    return urlunsplit(parts._replace(query=urlencode(query)))
