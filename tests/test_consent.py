"""Authorising a connector when you are already signed in.

Connecting an MCP client asked for the password every time, even in a browser
that was signed in and had been all along. Two things wrong with that: it is
friction on the one flow people repeat most, and it teaches someone to type
their password at whatever page an app happens to open.

Consent is still asked for every time, because that is the part that actually
needs a person. What is dropped is re-proving who they are when the browser
already knows.
"""

import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcphub import roles
from mcphub.app import create_app
from mcphub.auth.provider import SCOPE_USE
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow

BASE = "http://127.0.0.1:8080"
PASSWORD = "correct horse battery"
REDIRECT = "http://localhost:9999/callback"


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
        "updated_at) VALUES ('router', 'mcp-proxy', 'Living room router', 0, '{}', ?, ?)",
        (utcnow(), utcnow()))
    for name, admin in (("boss", 1), ("colleague", 0), ("stranger", 0)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, ?, 0, ?)", (name, hash_password(PASSWORD), admin, utcnow()))
    state.db.execute(
        "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
        "SELECT u.id, b.id, 'viewer', ? FROM users u, backends b "
        "WHERE u.username = 'colleague' AND b.slug = 'router'", (utcnow(),))
    await state.provider.register_client(OAuthClientInformationFull(
        client_id="claude", client_secret="s3cret", client_name="Claude",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"]))
    async with app.router.lifespan_context(app):
        yield state, app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                             follow_redirects=False)


async def park(state, resource: str | None = f"{BASE}/mcp/router") -> str:
    """An /authorize hop, as the provider parks one."""
    from mcp.server.auth.provider import AuthorizationParams

    url = await state.provider.authorize(
        await state.provider.get_client("claude"),
        AuthorizationParams(state="st", scopes=[SCOPE_USE], code_challenge="chal",
                            redirect_uri=AnyUrl(REDIRECT),
                            redirect_uri_provided_explicitly=True, resource=resource))
    return url.split("req=")[1]


async def signed_in(app, username: str = "colleague") -> httpx.AsyncClient:
    browser = client(app)
    await browser.post("/login", data={"username": username, "password": PASSWORD})
    return browser


# ── what a signed-in browser is asked ─────────────────────────────────────

async def test_it_asks_to_allow_rather_than_to_sign_in_again(hub):
    state, app = hub
    browser = await signed_in(app)
    page = await browser.get(f"/login?req={await park(state)}")

    assert page.status_code == 200
    assert "Allow Claude?" in page.text
    assert 'type="password"' not in page.text, "the password is not what this hop needs"


async def test_it_says_what_is_being_authorised(hub):
    """A consent screen that does not say what it is consenting to is a button."""
    state, app = hub
    browser = await signed_in(app)
    page = (await browser.get(f"/login?req={await park(state)}")).text

    assert "Living room router" in page
    assert "colleague" in page
    assert roles.VIEWER in page
    assert f"{BASE}/mcp/router" in page


async def test_a_browser_that_is_not_signed_in_still_gets_the_form(hub):
    state, app = hub
    page = await client(app).get(f"/login?req={await park(state)}")
    assert 'type="password"' in page.text


async def test_allowing_sends_a_code_back_to_the_client(hub):
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)
    page = await browser.get(f"/login?req={request_id}")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]

    done = await browser.post(f"/login?req={request_id}",
                              data={"consent": "allow", "csrf": csrf})
    assert done.status_code == 303
    query = parse_qs(urlparse(done.headers["location"]).query)
    assert query["state"] == ["st"]
    assert query["code"], "the client is sent an authorization code"


async def test_the_code_belongs_to_the_account_that_was_signed_in(hub):
    state, app = hub
    browser = await signed_in(app, "colleague")
    request_id = await park(state)
    csrf = (await browser.get(f"/login?req={request_id}")).text.split(
        'name="csrf" value="')[1].split('"')[0]
    done = await browser.post(f"/login?req={request_id}",
                              data={"consent": "allow", "csrf": csrf})

    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    loaded = await state.provider.load_authorization_code(
        await state.provider.get_client("claude"), code)
    assert loaded is not None and loaded.subject == "colleague"


# ── and what it refuses ───────────────────────────────────────────────────

async def test_nothing_is_minted_without_a_click(hub):
    """Consent is asked every time. Silently completing a flow would let any
    page the browser visits get a connector authorised."""
    state, app = hub
    browser = await signed_in(app)
    page = await browser.get(f"/login?req={await park(state)}")
    assert page.status_code == 200, "no redirect back to the client"
    assert not state.db.query("SELECT code FROM auth_codes")


async def test_a_form_from_another_site_is_refused(hub):
    """A click is exactly what another site can arrange. The token this form
    carries comes from the session cookie, which is httponly."""
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)
    await browser.get(f"/login?req={request_id}")

    forged = await browser.post(f"/login?req={request_id}",
                                data={"consent": "allow", "csrf": "guessed"})
    assert forged.status_code == 403
    assert not state.db.query("SELECT code FROM auth_codes")


async def test_cancelling_tells_the_client_rather_than_leaving_it_waiting(hub):
    """A window that never comes back can only be ended by timing out."""
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)
    csrf = (await browser.get(f"/login?req={request_id}")).text.split(
        'name="csrf" value="')[1].split('"')[0]

    done = await browser.post(f"/login?req={request_id}",
                              data={"consent": "deny", "csrf": csrf})
    assert done.status_code == 303
    query = parse_qs(urlparse(done.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["st"]
    assert not state.db.query("SELECT code FROM auth_codes")


async def test_an_account_with_no_grant_is_told_now_rather_than_later(hub):
    """A connector that authorises and then fails on every request is a much
    worse thing to debug than a refusal here."""
    state, app = hub
    browser = await signed_in(app, "stranger")
    page = await browser.get(f"/login?req={await park(state)}")
    assert page.status_code == 403
    assert "has not been granted" in page.text


async def test_a_grant_removed_while_deciding_is_still_checked(hub):
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)
    csrf = (await browser.get(f"/login?req={request_id}")).text.split(
        'name="csrf" value="')[1].split('"')[0]
    state.db.execute("DELETE FROM backend_grants")

    done = await browser.post(f"/login?req={request_id}",
                              data={"consent": "allow", "csrf": csrf})
    assert done.status_code == 403
    assert not state.db.query("SELECT code FROM auth_codes")


async def test_a_session_that_ended_while_deciding_asks_for_the_password(hub):
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)
    csrf = (await browser.get(f"/login?req={request_id}")).text.split(
        'name="csrf" value="')[1].split('"')[0]
    state.db.execute("DELETE FROM web_sessions")

    done = await browser.post(f"/login?req={request_id}",
                              data={"consent": "allow", "csrf": csrf})
    assert done.status_code == 401
    assert 'type="password"' in done.text


async def test_an_admin_needs_no_grant_to_authorise(hub):
    state, app = hub
    browser = await signed_in(app, "boss")
    page = await browser.get(f"/login?req={await park(state)}")
    assert page.status_code == 200
    assert roles.ADMIN in page.text


# ── switching account ─────────────────────────────────────────────────────

async def test_asking_for_another_account_ends_this_session_first(hub):
    """Otherwise the consent screen comes straight back, for the same account."""
    state, app = hub
    browser = await signed_in(app)
    request_id = await park(state)

    page = await browser.get(f"/login?req={request_id}&switch=1")
    assert 'type="password"' in page.text
    assert "mcphub_session" in page.headers.get("set-cookie", "")
    assert not state.db.query("SELECT token_hash FROM web_sessions")


async def test_signing_in_from_there_still_completes_the_hop(hub):
    state, app = hub
    browser = await signed_in(app, "stranger")
    request_id = await park(state)
    await browser.get(f"/login?req={request_id}&switch=1")

    done = await browser.post(f"/login?req={request_id}",
                              data={"username": "colleague", "password": PASSWORD})
    assert done.status_code == 303
    assert parse_qs(urlparse(done.headers["location"]).query)["code"]


# ── and the flow that never had a session ─────────────────────────────────

async def test_signing_in_cold_is_unchanged(hub):
    state, app = hub
    request_id = await park(state)
    done = await client(app).post(f"/login?req={request_id}",
                                  data={"username": "colleague", "password": PASSWORD})
    assert done.status_code == 303
    assert parse_qs(urlparse(done.headers["location"]).query)["code"]


async def test_an_expired_link_says_so(hub):
    state, app = hub
    browser = await signed_in(app)
    page = await browser.get("/login?req=nothing-like-that")
    assert page.status_code == 400
    assert "expired" in page.text
