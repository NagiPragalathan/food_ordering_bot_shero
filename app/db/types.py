"""Dialect portability shims for column types.

Production runs on Postgres and the models use real `JSONB`. SQLite has no
such type, so building the same tables there - for the test suite, and for a
local run with no Postgres installed - needs a compilation rule.

Importing this module registers the rule. `app.db.base` imports it, so any
code path that reaches a model has it in place; nothing has to remember to
call anything.

This only changes the DDL SQLAlchemy *emits* for SQLite. The Python-side
behaviour of a JSONB column (dict in, dict out) is unaffected, and on
Postgres nothing changes at all.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_on_sqlite(type_, compiler, **kw) -> str:
    """Render JSONB as SQLite's JSON, which is TEXT with JSON1 functions."""
    return "JSON"
