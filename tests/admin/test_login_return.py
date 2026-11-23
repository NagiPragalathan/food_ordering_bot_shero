"""Signing in from a page's own link returns to that page (e.g. /admin/sft),
not the dashboard; and nothing but this site's admin pages is a return."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import NotAuthenticated
from app.admin.routes import auth
from app.core import tenants
from app.db.models import AdminUser
from app.db.session import get_primary_session, get_session
from app.main import not_authenticated_handler


@pytest.fixture
def client(session, monkeypatch):
    async def ops(_session, email, password):
        if password == "right":
            return AdminUser(id=uuid.uuid4(), email=email, name="Ops", password_hash="x",
                             is_active=True)
        return None

    monkeypatch.setattr(auth, "authenticate", ops)
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.add_exception_handler(NotAuthenticated, not_authenticated_handler)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_primary_session] = lambda: session
    return TestClient(app, follow_redirects=False)


def _next(response) -> str | None:
    return (parse_qs(urlsplit(response.headers["location"]).query).get("next") or [None])[0]


def test_a_signed_out_visit_to_sft_remembers_the_page(client):
    response = client.get("/admin/sft?tab=settings")
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"
    assert _next(response) == "/admin/sft?tab=settings"

    form = client.get("/admin/login?next=/admin/sft?tab=settings").text
    assert 'name="next" value="/admin/sft?tab=settings"' in form
    assert "continue to Shero Food Training" in form


def test_signing_in_goes_back_to_that_page(client):
    response = client.post("/admin/login", data={"email": "ops@shero.test", "password": "right",
                                                 "next": "/admin/sft?tab=chat"})
    assert response.status_code == 303 and response.headers["location"] == "/admin/sft?tab=chat"


def test_without_a_page_it_is_the_dashboard_as_before(client):
    response = client.post("/admin/login", data={"email": "ops@shero.test", "password": "right"})
    assert urlsplit(response.headers["location"]).path == "/admin/"


def test_a_wrong_password_keeps_the_page_to_return_to(client):
    response = client.post("/admin/login", data={"email": "ops@shero.test", "password": "no",
                                                 "next": "/admin/sft"})
    assert response.status_code == 401 and 'name="next" value="/admin/sft"' in response.text


def test_signing_out_of_sft_comes_back_to_sft(client):
    response = client.get("/admin/logout?next=/admin/sft")
    assert _next(response) == "/admin/sft"


@pytest.mark.parametrize("bad", ["https://evil.example/admin", "//evil.example/admin",
                                 "/\\evil.example/admin", "/order/abc", "/admin/login",
                                 "/admin/logout", "admin/sft", ""])
def test_only_this_sites_admin_pages_are_a_return(client, bad):
    assert auth.safe_next(bad) is None
    response = client.post("/admin/login", data={"email": "ops@shero.test", "password": "right",
                                                 "next": bad})
    assert urlsplit(response.headers["location"]).path == "/admin/"


def test_dst_pages_keep_their_prefix(client):
    assert _next(client.get("/dst/admin/orders")) == "/dst/admin/orders"
