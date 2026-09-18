"""What removing a backend has to take with it.

Deleting was two statements: unmount, then delete the row. Grants and pins go
with it through ON DELETE CASCADE, which is real and verified below. Two things
did not: a plugin never heard that a backend it had provisioned things for was
going away, and the credentials minted for its endpoint stayed in the database
with nothing left to point at.
"""

import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password, hash_token
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, CheckResult, PluginDefaults
from mcphub.web.routes import _revoke_backend_credentials, _save_backend

PASSWORD = "correct horse battery"


class Provisioning(PluginDefaults):
    """A plugin with something to clean up, and a switch to make it fail."""

    id, name, description, fields = "provisioning", "Provisioning", "d", ()

    def __init__(self):
        self.released: list[BackendInstance] = []
        self.explode = False

    def build(self, instance):  # pragma: no cover - never mounted here
        raise AssertionError

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")

    async def on_delete(self, instance):
        if self.explode:
            raise RuntimeError("the remote API said no")
        self.released.append(instance)


@pytest.fixture
def hub_app():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://localhost:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    hub = app.state.hub
    hub.registry.register(Provisioning())
    hub.db.execute(
        "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
        "VALUES ('boss', ?, 1, 1, ?)", (hash_password(PASSWORD), utcnow()))
    return app


@pytest.fixture
async def signed_in(hub_app):
    transport = httpx.ASGITransport(app=hub_app)
    async with hub_app.router.lifespan_context(hub_app):
        client = httpx.AsyncClient(transport=transport, base_url="http://localhost:8080")
        await client.post("/login", data={"username": "boss", "password": PASSWORD})
        yield hub_app.state.hub, client


def make_backend(hub, slug="router", plugin_id="provisioning"):
    _save_backend(hub, slug=slug, plugin_id=plugin_id, title="Router", enabled=False,
                  config={"host": "10.0.0.1"}, secrets={"token": "s3cret"})
    return hub.backend_row(slug)["id"]


def mint_credentials(hub, slug, user_id=1):
    resource = f"http://localhost:8080/mcp/{slug}"
    hub.db.execute(
        "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, "
        "expires_at, created_at) VALUES (?, 'access', 'c', ?, 'use', ?, 9e9, ?)",
        (hash_token(f"tok-{slug}"), user_id, resource, utcnow()))
    hub.db.execute(
        "INSERT INTO auth_codes (code, client_id, user_id, redirect_uri, explicit_uri, "
        "code_challenge, scopes, resource, expires_at) VALUES (?, 'c', ?, 'u', 1, 'x', 'use', ?, 9e9)",
        (f"code-{slug}", user_id, resource))


def credentials_for(hub, slug):
    resource = f"http://localhost:8080/mcp/{slug}"
    return (hub.db.one("SELECT COUNT(*) n FROM tokens WHERE resource = ?", (resource,))["n"]
            + hub.db.one("SELECT COUNT(*) n FROM auth_codes WHERE resource = ?", (resource,))["n"])


# ── the plugin gets told ──────────────────────────────────────────────────

async def test_the_plugin_is_told_before_the_row_goes(signed_in):
    hub, client = signed_in
    make_backend(hub)
    await client.post("/backends/router/delete")
    assert len(hub.registry.get("provisioning").released) == 1


async def test_the_plugin_still_has_its_configuration_when_told(signed_in):
    """Its last chance to undo what it set up, while it still holds the
    credentials to do it with."""
    hub, client = signed_in
    make_backend(hub)
    await client.post("/backends/router/delete")
    released = hub.registry.get("provisioning").released[0]
    assert released.config["host"] == "10.0.0.1"
    assert released.secrets["token"] == "s3cret", "the secrets were already gone"


async def test_a_cleanup_failure_does_not_block_the_removal(signed_in):
    """A removal the user already asked for is not the plugin's to veto, and a
    hook that always raised would leave no way out but editing the database."""
    hub, client = signed_in
    make_backend(hub)
    hub.registry.get("provisioning").explode = True
    await client.post("/backends/router/delete")
    assert hub.backend_row("router") is None


async def test_a_cleanup_failure_is_reported_rather_than_swallowed(signed_in):
    """Silence here means an API token left registered somewhere that nobody
    knows about."""
    hub, client = signed_in
    make_backend(hub)
    hub.registry.get("provisioning").explode = True
    response = await client.post("/backends/router/delete", follow_redirects=True)
    assert "could not finish cleaning up" in response.text
    assert "the remote API said no" in response.text


async def test_an_uninstalled_plugin_is_called_out(signed_in):
    """Nothing can run its cleanup, so say so rather than removing quietly."""
    hub, client = signed_in
    make_backend(hub, plugin_id="long-since-uninstalled")
    response = await client.post("/backends/router/delete", follow_redirects=True)
    assert "is not installed" in response.text
    assert hub.backend_row("router") is None


async def test_a_clean_removal_says_nothing(signed_in):
    hub, client = signed_in
    make_backend(hub)
    response = await client.post("/backends/router/delete", follow_redirects=True)
    assert "could not finish" not in response.text


# ── credentials go with it ────────────────────────────────────────────────

async def test_the_endpoints_credentials_are_released(signed_in):
    hub, client = signed_in
    make_backend(hub)
    mint_credentials(hub, "router")
    assert credentials_for(hub, "router") == 2
    await client.post("/backends/router/delete")
    assert credentials_for(hub, "router") == 0


async def test_another_backends_credentials_are_left_alone(signed_in):
    hub, client = signed_in
    make_backend(hub)
    make_backend(hub, slug="keeper")
    mint_credentials(hub, "router")
    mint_credentials(hub, "keeper")
    await client.post("/backends/router/delete")
    assert credentials_for(hub, "keeper") == 2


def test_a_credential_minted_under_an_older_public_url_is_still_recognised(hub_app):
    """Matched through the same rule authorization uses, not by comparing whole
    URLs — the hub's public URL can change, and those tokens are still its own."""
    hub = hub_app.state.hub
    make_backend(hub)
    hub.db.execute(
        "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, "
        "expires_at, created_at) VALUES (?, 'access', 'c', 1, 'use', ?, 9e9, ?)",
        (hash_token("old"), "https://mcp.example.com/mcp/router", utcnow()))
    assert _revoke_backend_credentials(hub, "router") == 1


def test_a_token_with_no_resource_is_left_alone(hub_app):
    """Not every token is pinned to a backend; one that is not belongs to none
    of them and must survive any deletion."""
    hub = hub_app.state.hub
    make_backend(hub)
    hub.db.execute(
        "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, "
        "expires_at, created_at) VALUES (?, 'access', 'c', 1, 'use', NULL, 9e9, ?)",
        (hash_token("unpinned"), utcnow()))
    _revoke_backend_credentials(hub, "router")
    assert hub.db.one("SELECT COUNT(*) n FROM tokens")["n"] == 1


# ── what already worked, pinned down ──────────────────────────────────────

async def test_grants_and_pins_still_cascade(signed_in):
    """Declared ON DELETE CASCADE, and the pragma that makes SQLite honour it
    is set on the one connection — verified here rather than assumed."""
    hub, client = signed_in
    backend_id = make_backend(hub)
    user_id = hub.db.one("SELECT id FROM users")["id"]
    hub.db.execute("INSERT INTO backend_grants (user_id, backend_id, created_at) VALUES (?,?,?)",
                   (user_id, backend_id, utcnow()))
    hub.db.execute("INSERT INTO backend_pins (user_id, backend_id, version, created_at) "
                   "VALUES (?,?,?,?)", (user_id, backend_id, "1.0", utcnow()))

    await client.post("/backends/router/delete")
    assert hub.db.one("SELECT COUNT(*) n FROM backend_grants")["n"] == 0
    assert hub.db.one("SELECT COUNT(*) n FROM backend_pins")["n"] == 0


async def test_deleting_something_that_is_already_gone_is_harmless(signed_in):
    hub, client = signed_in
    response = await client.post("/backends/never-existed/delete")
    assert response.status_code == 303
