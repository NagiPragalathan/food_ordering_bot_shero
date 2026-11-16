"""Comments on the dashboard's pages (admin/routes/feedback.py, services/page_comments.py):
reviewers comment on any element, everyone sees them on the Feedback page."""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.core import tenants
from app.db.models import AdminUser
from app.db.session import get_primary_session, get_session
from app.services import page_comments as pc

BOSS = "boss@shero.test"


@pytest.fixture(autouse=True)
def fresh_reviewers(monkeypatch):
    monkeypatch.setattr(pc, "_reviewers", set())
    monkeypatch.setattr(pc, "_loaded_at", 0.0)


@pytest.fixture
async def users(session):
    boss = AdminUser(email=BOSS, name="Boss", password_hash="x", is_active=True)
    dev = AdminUser(email="dev@shero.test", name="Dev", password_hash="x", is_active=True)
    session.add_all([boss, dev])
    await session.flush()
    return boss, dev


def client_for(session, user) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_primary_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app, follow_redirects=False)


def config(html: str) -> dict:
    found = re.search(r'<script id="page-comments-config" type="application/json">(.*?)</script>',
                      html, re.S)
    return json.loads(found.group(1))


COMMENT = {
    "path": "/admin/orders", "query": "?status=paid", "page_title": "Orders · Shero US Admin",
    "template": "app/templates/admin/orders.html", "route_file": "app/admin/routes/orders.py",
    "selector": "body > div > table > tbody > tr:nth-of-type(1) > td[data-order-address]:nth-of-type(3)",
    "element_label": 'table cell in the "Customer and address" column: "Lavanya Mohan"',
    "element_html": '<td class="px-4 py-3" data-order-address>Lavanya Mohan</td>',
    "x_pct": 0.4, "y_pct": 0.2, "page_x": 640, "page_y": 310, "viewport_width": 1536,
    "body": "Show the full address on one line, with the PIN code.",
}


async def test_only_reviewers_see_the_comment_toggle(session, users):
    boss, _ = users
    page = client_for(session, boss).get("/admin/orders").text
    assert "data-comment-toggle" not in page and config(page)["can_comment"] is False
    assert 'href="/admin/feedback"' in page or "/admin/feedback" in page   # Feedback: everyone

    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    page = client_for(session, boss).get("/admin/orders").text
    cfg = config(page)
    assert "data-comment-toggle" in page and cfg["can_comment"] is True
    # The page knows what draws it, for the AI copy.
    assert cfg["template"] == "app/templates/admin/orders.html"
    assert cfg["route_file"] == "app/admin/routes/orders.py"
    assert cfg["api"] == "/admin/feedback/api/comments"


async def test_a_reviewer_pins_a_comment_and_it_is_listed(session, users):
    boss, dev = users
    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    made = client_for(session, boss).post("/admin/feedback/api/comments", json=COMMENT)
    assert made.status_code == 200
    thread = made.json()["thread"]
    assert thread["status"] == "pending" and thread["author"]["name"] == "Boss"
    assert thread["business"] == "us"

    listed = client_for(session, dev).get("/admin/feedback/api/comments",
                                         params={"path": "/admin/orders"}).json()
    assert [t["id"] for t in listed["threads"]] == [thread["id"]]
    assert listed["can_comment"] is False        # the developer reads; only reviewers pin

    page = client_for(session, dev).get("/admin/feedback").text
    assert "Show the full address on one line" in page and "Orders" in page
    assert f"#comment-{thread['id']}" in page                     # Show on page
    assert f'data-copy-source="copy-ai-{thread["id"]}"' in page   # Copy for AI
    assert f'data-copy-source="copy-text-{thread["id"]}"' in page  # Copy as text


async def test_someone_not_a_reviewer_cannot_pin_comments(session, users):
    _, dev = users
    refused = client_for(session, dev).post("/admin/feedback/api/comments", json=COMMENT)
    assert refused.status_code == 403


async def test_a_comment_goes_pending_in_progress_done(session, users):
    boss, dev = users
    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    thread = client_for(session, boss).post("/admin/feedback/api/comments", json=COMMENT).json()["thread"]
    dev_client = client_for(session, dev)

    replied = dev_client.post(f"/admin/feedback/api/comments/{thread['id']}/replies",
                              json={"body": "On it - today."}).json()["thread"]
    assert [r["body"] for r in replied["replies"]] == ["On it - today."]

    moving = dev_client.post(f"/admin/feedback/api/comments/{thread['id']}/status",
                             json={"status": "in_progress"}).json()["thread"]
    assert moving["status"] == "in_progress" and moving["status_label"] == "In progress"
    done = dev_client.post(f"/admin/feedback/comments/{thread['id']}/status",
                           data={"status": "done"})
    assert done.status_code == 303

    page = dev_client.get("/admin/feedback?status=done").text
    assert "Show the full address on one line" in page
    assert "Show the full address" not in dev_client.get("/admin/feedback?status=pending").text
    assert dev_client.post(f"/admin/feedback/api/comments/{thread['id']}/status",
                           json={"status": "maybe"}).status_code == 400


async def test_copy_for_ai_names_the_exact_files_and_element(session, users):
    boss, _ = users
    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    thread = client_for(session, boss).post("/admin/feedback/api/comments", json=COMMENT).json()["thread"]
    ai, text = thread["copy"]["ai"], thread["copy"]["text"]
    for needed in ("URL path: /admin/orders?status=paid",
                   "Template file: app/templates/admin/orders.html",
                   "Route file: app/admin/routes/orders.py",
                   "CSS selector: body > div > table",
                   'Element: table cell in the "Customer and address" column',
                   "data-order-address",
                   "Show the full address on one line, with the PIN code.",
                   "Shero US under /admin and DST India under /dst/admin"):
        assert needed in ai, needed
    # The plain copy reads like a message: no file paths or selectors.
    assert text.startswith("Orders page (Shero US) - /admin/orders?status=paid")
    assert "Boss: Show the full address on one line" in text
    assert "app/templates" not in text and "CSS selector" not in text


async def test_every_admin_login_has_a_reviewer_switch(session, users):
    boss, dev = users
    admin = client_for(session, dev)
    page = admin.get("/admin/feedback").text
    assert f'data-reviewer="{BOSS}"' in page and 'data-reviewer="dev@shero.test"' in page

    on = admin.post("/admin/feedback/reviewers", data={"email": BOSS, "reviewer": "on"})
    assert on.status_code == 303 and pc.can_comment(BOSS)
    assert 'aria-checked="true"' in admin.get("/admin/feedback").text

    off = admin.post("/admin/feedback/reviewers", data={"email": BOSS})
    assert off.status_code == 303 and not pc.can_comment(BOSS)
    # Only an admin login can be made a reviewer.
    admin.post("/admin/feedback/reviewers", data={"email": "stranger@x.com", "reviewer": "on"})
    assert not pc.can_comment("stranger@x.com")


async def test_a_comment_on_a_dst_page_belongs_to_dst_india(session, users):
    boss, _ = users
    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    reviewer = client_for(session, boss)
    page = reviewer.get("/dst/admin/menu").text
    assert config(page)["api"] == "/dst/admin/feedback/api/comments"
    made = reviewer.post("/dst/admin/feedback/api/comments",
                         json={**COMMENT, "path": "/dst/admin/menu", "query": "",
                               "page_title": "Menu · DST India Admin"}).json()["thread"]
    assert made["business"] == "dst"
    assert "Menu page (DST India) - /dst/admin/menu" in made["copy"]["text"]
    listed = reviewer.get("/admin/feedback?business=dst").text
    assert "Show the full address on one line" in listed


async def test_a_comment_needs_words(session, users):
    boss, _ = users
    await pc.add_reviewer(session, BOSS, added_by="dev@shero.test")
    empty = client_for(session, boss).post("/admin/feedback/api/comments", json={**COMMENT, "body": "  "})
    assert empty.status_code == 400 and "Write what should change" in empty.json()["error"]
