"""Getting an account back in, and cutting one off.

There is no email here, so a forgotten password has exactly one way out: an
administrator sets a new one and hands it over. Without that, an account whose
password is lost is an account that has to be deleted and rebuilt, grants and
all.

Reset and revoke are deliberately two buttons. A forgotten password is the
ordinary case and breaking every connector over it would make it the button
nobody presses; a password that got out is the other case, and then cutting the
connectors is the whole point. One action doing both, silently, would be wrong
for whichever case you were actually in.
"""

import re
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest

from mcphub import appaccess, roles
from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password, hash_token, new_password, verify_password
from mcphub.db import utcnow

BASE = "http://127.0.0.1:8080"
PASSWORD = "correct horse battery"


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
        "updated_at) VALUES ('router', 'mcp-proxy', 'Router', 0, '{}', ?, ?)",
        (utcnow(), utcnow()))
    for name, admin in (("boss", 1), ("colleague", 0)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, "
            "created_at) VALUES (?, ?, ?, 0, ?)",
            (name, hash_password(PASSWORD), admin, utcnow()))
    async with app.router.lifespan_context(app):
        yield state, app


def who(state, username: str) -> int:
    return int(state.db.one("SELECT id FROM users WHERE username = ?", (username,))["id"])


async def signed_in(app, username: str = "boss") -> httpx.AsyncClient:
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                follow_redirects=False)
    await browser.post("/login", data={"username": username, "password": PASSWORD})
    return browser


def issued_password(page: str) -> str:
    match = re.search(r'<code class="secret" id="issued">([^<]+)</code>', page)
    assert match, "the new password has to be shown, or the reset achieved nothing"
    return match.group(1)


def give_them_a_token(state, username: str, slug: str = "router") -> str:
    token = f"tok-{username}"
    state.db.execute(
        "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, "
        "expires_at, created_at) VALUES (?, 'access', 'c', ?, 'mcp:use', ?, 9e9, ?)",
        (hash_token(token), who(state, username), f"{BASE}/mcp/{slug}", utcnow()))
    return token


# ── the generated password ────────────────────────────────────────────────

def test_it_is_long_enough_for_the_rule_it_has_to_pass():
    """The account form refuses anything under twelve characters; a generated
    one that could not be typed back in would be a fine joke."""
    assert len(new_password()) >= 12


def test_two_of_them_are_not_the_same():
    assert len({new_password() for _ in range(50)}) == 50


def test_it_holds_no_character_that_is_read_wrong():
    """Someone is about to copy this off a screen. `l` and `1` cost a support
    conversation that the alphabet can avoid entirely."""
    assert not set(new_password()) & set("lo01IO")


def test_it_is_grouped_so_a_missed_character_shows():
    assert new_password().count("-") >= 3


# ── resetting ─────────────────────────────────────────────────────────────

async def test_the_new_password_works(hub):
    state, app = hub
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/{who(state, 'colleague')}/password")

    assert page.status_code == 200
    fresh = issued_password(page.text)
    assert state.provider.authenticate_user("colleague", fresh) is not None


async def test_the_old_one_stops(hub):
    state, app = hub
    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'colleague')}/password")
    assert state.provider.authenticate_user("colleague", PASSWORD) is None


async def test_only_that_account_changes(hub):
    state, app = hub
    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'colleague')}/password")
    assert state.provider.authenticate_user("boss", PASSWORD) is not None


async def test_the_hub_keeps_only_a_hash_of_it(hub):
    state, app = hub
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/{who(state, 'colleague')}/password")
    fresh = issued_password(page.text)

    stored = state.db.one("SELECT password_hash FROM users WHERE username = 'colleague'")
    assert fresh not in stored["password_hash"]
    assert verify_password(fresh, stored["password_hash"])


async def test_it_is_never_put_in_a_url(hub):
    """A query string is written into history, logs and referrers, which is the
    one place a password must not be. So this renders rather than redirects."""
    state, app = hub
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/{who(state, 'colleague')}/password")
    assert page.status_code == 200, "a 303 would have to carry it somewhere"
    assert "location" not in page.headers


async def test_their_other_sessions_end(hub):
    state, app = hub
    theirs = await signed_in(app, "colleague")
    assert (await theirs.get("/account")).status_code == 200

    boss = await signed_in(app)
    await boss.post(f"/accounts/{who(state, 'colleague')}/password")
    assert (await theirs.get("/account")).status_code == 303


async def test_connectors_they_authorised_keep_working(hub):
    """A forgotten password is the ordinary case. Breaking every connector over
    it would make this the button nobody presses, and `Revoke connectors` is
    right beside it for when that is what is meant."""
    state, app = hub
    give_them_a_token(state, "colleague")
    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'colleague')}/password")

    assert state.db.query("SELECT token_hash FROM tokens WHERE user_id = ?",
                          (who(state, "colleague"),))


async def test_an_admin_resetting_their_own_is_not_logged_out_by_it(hub):
    """Their click should not be the thing that locks them out of the page they
    clicked it on."""
    state, app = hub
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/{who(state, 'boss')}/password")
    assert page.status_code == 200
    assert (await browser.get("/accounts")).status_code == 200


async def test_an_app_has_no_password_to_reset(hub):
    state, app = hub
    state.apps.set_grants("aenvae", {"router": roles.VIEWER})
    browser = await signed_in(app)
    page = await browser.post(
        f"/accounts/{who(state, appaccess.account_name('aenvae'))}/password")
    assert "no password" in page.text
    assert state.db.one("SELECT password_hash FROM users WHERE username = 'app:aenvae'"
                        )["password_hash"] == appaccess.NO_PASSWORD


# ── revoking ──────────────────────────────────────────────────────────────

async def test_revoking_cuts_every_connector(hub):
    state, app = hub
    give_them_a_token(state, "colleague")
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/{who(state, 'colleague')}/revoke")

    assert page.status_code == 200
    assert not state.db.query("SELECT token_hash FROM tokens WHERE user_id = ?",
                              (who(state, "colleague"),))


async def test_revoking_leaves_the_password_alone(hub):
    """The other half of keeping them two buttons: this one is for a token that
    got out, and an account that then cannot sign in has been punished twice."""
    state, app = hub
    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'colleague')}/revoke")
    assert state.provider.authenticate_user("colleague", PASSWORD) is not None


async def test_revoking_leaves_their_grants_alone(hub):
    state, app = hub
    state.db.execute(
        "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
        "SELECT ?, id, 'viewer', ? FROM backends WHERE slug = 'router'",
        (who(state, "colleague"), utcnow()))
    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'colleague')}/revoke")
    assert state.db.query("SELECT role FROM backend_grants WHERE user_id = ?",
                          (who(state, "colleague"),))


async def test_revoking_an_app_takes_back_the_copy_it_is_holding(hub):
    """An app's credentials live in memory as well as hashed in the database,
    and only the memory copy is the one it is actually using."""
    state, app = hub
    state.apps.set_grants("aenvae", {"router": roles.VIEWER})
    before = state.apps.issue("aenvae")["router"]["token"]

    browser = await signed_in(app)
    await browser.post(f"/accounts/{who(state, 'app:aenvae')}/revoke")
    assert state.apps.issue("aenvae")["router"]["token"] != before


# ── who may do it ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["password", "revoke"])
async def test_an_ordinary_account_cannot(hub, action):
    state, app = hub
    browser = await signed_in(app, "colleague")
    page = await browser.post(f"/accounts/{who(state, 'boss')}/{action}")
    assert page.status_code == 403
    assert state.provider.authenticate_user("boss", PASSWORD) is not None


@pytest.mark.parametrize("action", ["password", "revoke"])
async def test_signing_in_is_required(hub, action):
    state, app = hub
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                follow_redirects=False)
    page = await browser.post(f"/accounts/{who(state, 'boss')}/{action}")
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")


@pytest.mark.parametrize("action", ["password", "revoke"])
async def test_an_account_that_is_not_there_says_so(hub, action):
    state, app = hub
    browser = await signed_in(app)
    page = await browser.post(f"/accounts/9999/{action}")
    assert page.status_code == 404
