"""The Zoho OAuth connect flow.

The risk here is silent: a wrong data centre, a redirect URI that does not
match the registered one, or a missing `access_type=offline` all produce a
flow that looks like it worked and then has no usable refresh token.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.exceptions import IntegrationError
from app.services import zoho_connect


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


@pytest.mark.parametrize("centre,host", [
    ("com", "accounts.zoho.com"),
    ("in", "accounts.zoho.in"),
    ("eu", "accounts.zoho.eu"),
    ("au", "accounts.zoho.com.au"),
    ("jp", "accounts.zoho.jp"),
    ("ca", "accounts.zohocloud.ca"),
])
def test_each_data_centre_authorises_against_its_own_host(centre, host):
    """Zoho's data centres are isolated - the wrong host 401s every call."""
    url = zoho_connect.authorize_url("1000.ABC", centre, "state123")
    assert urlparse(url).netloc == host


def test_an_unknown_data_centre_falls_back_to_com_rather_than_crashing():
    url = zoho_connect.authorize_url("1000.ABC", "atlantis", "s")
    assert urlparse(url).netloc == "accounts.zoho.com"


def test_offline_access_is_requested_or_no_refresh_token_comes_back():
    """`access_type=offline` is what makes Zoho issue a refresh token."""
    params = _query(zoho_connect.authorize_url("1000.ABC", "com", "s"))
    assert params["access_type"] == "offline"


def test_consent_is_forced_so_reconnecting_still_yields_a_token():
    """Without prompt=consent a second connect returns no refresh token."""
    params = _query(zoho_connect.authorize_url("1000.ABC", "com", "s"))
    assert params["prompt"] == "consent"


def test_the_state_is_carried_through_and_is_not_guessable():
    params = _query(zoho_connect.authorize_url("1000.ABC", "com", "opaque-state"))
    assert params["state"] == "opaque-state"
    assert zoho_connect.new_state() != zoho_connect.new_state()
    assert len(zoho_connect.new_state()) >= 24


def test_the_authorize_and_token_calls_use_the_same_redirect_uri():
    """Zoho compares them; a mismatch fails at the exchange, not before."""
    params = _query(zoho_connect.authorize_url("1000.ABC", "com", "s"))
    assert params["redirect_uri"] == zoho_connect.redirect_uri()
    assert zoho_connect.redirect_uri().endswith("/admin/settings/zoho/callback")


def test_the_requested_scopes_cover_what_the_bot_actually_does():
    params = _query(zoho_connect.authorize_url("1000.ABC", "com", "s"))
    assert "ZohoCRM.modules.ALL" in params["scope"]
    assert "ZohoCRM.settings.ALL" in params["scope"]


# --- the token exchange ------------------------------------------------------
def _mock_post(monkeypatch, payload, status_code=200):
    """Stand in for the one POST `exchange_code` makes."""
    async def post(self, url, data=None, **kwargs):
        return httpx.Response(status_code, json=payload,
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)


@pytest.mark.asyncio
async def test_a_refresh_token_comes_back_on_success(monkeypatch):
    _mock_post(monkeypatch, {"refresh_token": "1000.rt", "access_token": "1000.at"})
    token = await zoho_connect.exchange_code("code", "id", "secret", "com")
    assert token == "1000.rt"


@pytest.mark.asyncio
async def test_zoho_errors_arrive_inside_a_200_and_are_still_raised(monkeypatch):
    """Zoho reports OAuth failures with HTTP 200, so status is not enough."""
    _mock_post(monkeypatch, {"error": "invalid_client"}, status_code=200)
    with pytest.raises(IntegrationError) as exc:
        await zoho_connect.exchange_code("code", "id", "wrong", "com")
    assert "Client ID or Client Secret" in exc.value.message


@pytest.mark.asyncio
async def test_an_expired_grant_token_says_so_plainly(monkeypatch):
    _mock_post(monkeypatch, {"error": "invalid_code"})
    with pytest.raises(IntegrationError) as exc:
        await zoho_connect.exchange_code("stale", "id", "secret", "com")
    assert "10 minutes" in exc.value.message


@pytest.mark.asyncio
async def test_a_redirect_mismatch_names_the_uri_to_register(monkeypatch):
    _mock_post(monkeypatch, {"error": "redirect_uri_mismatch"})
    with pytest.raises(IntegrationError) as exc:
        await zoho_connect.exchange_code("code", "id", "secret", "com")
    assert zoho_connect.redirect_uri() in exc.value.message


@pytest.mark.asyncio
async def test_an_access_token_without_a_refresh_token_is_an_error(monkeypatch):
    """The quiet failure: consent already granted, so no refresh token."""
    _mock_post(monkeypatch, {"access_token": "1000.at"})
    with pytest.raises(IntegrationError) as exc:
        await zoho_connect.exchange_code("code", "id", "secret", "com")
    assert "Connected Apps" in exc.value.message


@pytest.mark.asyncio
async def test_a_network_failure_is_reported_not_swallowed(monkeypatch):
    async def post(self, url, data=None, **kwargs):
        raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with pytest.raises(IntegrationError) as exc:
        await zoho_connect.exchange_code("code", "id", "secret", "com")
    assert "Could not reach Zoho" in exc.value.message


# --- data centre from Zoho's reply -------------------------------------------
@pytest.mark.parametrize("location,centre", [
    ("us", "com"), ("in", "in"), ("IN", "in"), ("eu", "eu"),
    ("au", "au"), ("jp", "jp"), ("ca", "ca"),
])
def test_the_callback_location_names_the_data_centre(location, centre):
    assert zoho_connect.centre_from_location(location) == centre


@pytest.mark.parametrize("location", [None, "", "evil.example", "https://accounts.attacker"])
def test_an_unknown_location_is_not_trusted(location):
    """The secret is posted to the centre's host, so the URL must not pick it."""
    assert zoho_connect.centre_from_location(location) is None


# --- after connecting --------------------------------------------------------
@pytest.mark.asyncio
async def test_the_api_client_follows_a_data_centre_saved_while_running(monkeypatch):
    """Built at import on .com, it must call zohoapis.in once .in is saved."""
    from app.core.config import settings
    from app.integrations.zoho.client import ZohoClient

    monkeypatch.setattr(settings, "zoho_data_center", "com")
    client = ZohoClient()
    first = await client._get_client()
    assert str(first.base_url).startswith("https://www.zohoapis.com")

    monkeypatch.setattr(settings, "zoho_data_center", "in")
    second = await client._get_client()
    assert str(second.base_url).startswith("https://www.zohoapis.in")
    assert first.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_the_token_refresh_keeps_secrets_out_of_the_url(monkeypatch):
    from app.core.config import settings
    from app.integrations.zoho import oauth

    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "shh")
    monkeypatch.setattr(settings, "zoho_refresh_token", "rt")
    seen = {}

    async def post(self, url, params=None, data=None, **kwargs):
        seen.update(url=str(url), params=params, data=data)
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    oauth.invalidate_token()
    assert await oauth.get_access_token(force_refresh=True) == "at"
    assert "shh" not in seen["url"] and not seen["params"]
    assert seen["data"]["client_secret"] == "shh"
    oauth.invalidate_token()
