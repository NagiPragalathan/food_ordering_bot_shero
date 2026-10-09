"""Running on MySQL, the database on the AWS server (deploy/docker-compose.prod.yml):
the URL form, and the table definitions MySQL is given."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.db.models import Base
from app.db.session import _engine_options
from app.db.types import json_server_default
from app.db.url import driver_url


@pytest.mark.parametrize("given,expected", [
    ("mysql://shero:pw@mysql:3306/shero",
     "mysql+aiomysql://shero:pw@mysql:3306/shero?charset=utf8mb4"),
    ("mysql://shero:pw@mysql:3306/shero?charset=latin1",
     "mysql+aiomysql://shero:pw@mysql:3306/shero?charset=latin1"),
    ("mysql+aiomysql://shero:pw@mysql/shero", "mysql+aiomysql://shero:pw@mysql/shero"),
])
def test_mysql_urls_become_aiomysql_urls(given, expected):
    assert driver_url(given) == expected


def _ddl(table: str, dialect) -> str:
    return str(CreateTable(Base.metadata.tables[table]).compile(dialect=dialect))


def test_mysql_gets_json_and_microsecond_times():
    ddl = _ddl("orders", mysql.dialect())
    assert "JSONB" not in ddl and " JSON NOT NULL" in ddl
    assert "DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)" in ddl
    assert "CHAR(32)" in ddl          # UUID primary key


def test_other_databases_are_unchanged():
    assert "JSONB" in _ddl("orders", postgresql.dialect())
    assert "now()" in _ddl("orders", postgresql.dialect())
    assert "DATETIME(6)" not in _ddl("orders", sqlite.dialect())


def test_json_defaults_are_bracketed_only_on_mysql():
    assert str(json_server_default("[]", "mysql")) == "('[]')"
    assert str(json_server_default("{}", "postgresql")) == "'{}'"
    assert str(json_server_default("{}", "sqlite")) == "'{}'"


def test_mysql_connections_are_renewed_before_mysql_drops_them():
    assert _engine_options("mysql+aiomysql://u:p@h/db")["pool_recycle"] == 1800
    assert "pool_recycle" not in _engine_options("postgresql+asyncpg://u:p@h/db")
