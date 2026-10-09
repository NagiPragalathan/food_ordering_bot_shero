"""Dialect portability shims for column types.

The models use Postgres's `JSONB`. SQLite (the tests, a local run) and MySQL
(the AWS server) have no such type, so building the same tables there needs a
compilation rule: both render it as their own `JSON`.

Importing this module registers the rule. `app.db.base` imports it, so any
code path that reaches a model has it in place; nothing has to remember to
call anything.

This only changes the DDL SQLAlchemy *emits* for SQLite and MySQL. The Python-side
behaviour of a JSONB column (dict in, dict out) is unaffected, and on
Postgres nothing changes at all.
"""

from __future__ import annotations

from sqlalchemy import DateTime, TextClause, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import functions


@compiles(JSONB, "sqlite")
def _compile_jsonb_on_sqlite(type_, compiler, **kw) -> str:
    """Render JSONB as SQLite's JSON, which is TEXT with JSON1 functions."""
    return "JSON"


@compiles(JSONB, "mysql")
def _compile_jsonb_on_mysql(type_, compiler, **kw) -> str:
    """Render JSONB as MySQL's native JSON type."""
    return "JSON"


@compiles(DateTime, "mysql")
def _compile_datetime_on_mysql(type_, compiler, **kw) -> str:
    """Microsecond precision, as on Postgres and SQLite.

    MySQL's plain DATETIME keeps whole seconds, which would let two messages
    written in the same second swap places when sorted by time.
    """
    return "DATETIME(6)"


@compiles(functions.now, "mysql")
def _compile_now_on_mysql(element, compiler, **kw) -> str:
    """now() at the same precision; MySQL refuses DEFAULT now() on DATETIME(6)."""
    return "CURRENT_TIMESTAMP(6)"


def json_server_default(literal: str, dialect: str) -> TextClause:
    """A constant default ('[]', '{}') for a JSON column, for migrations.

    MySQL accepts a default on a JSON column only as an expression in
    brackets - DEFAULT ('[]'); Postgres and SQLite take the plain literal.
    """
    quoted = f"'{literal}'"
    return text(f"({quoted})" if dialect == "mysql" else quoted)
