"""Proving a backend works before committing it.

The Test button lived only on the dashboard, and `backend_test` read the row
from the database — so it could only ever test what was already saved. The loop
that left was: type the credentials, save (which mounts, and can fail), go to
the dashboard, Test, come back to settings, fix. On a *new* backend there was
no row at all, so there was nothing to press.

These drive the real ASGI app rather than the handler, because the things worth
pinning down here — who is allowed to ask, what the form body turns into, that
nothing is written — are all properties of the request, not of a function.
"""

import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, CheckResult, ConfigField, PluginDefaults
from mcphub.plugins.builtin.hub import SLUG as HUB_SLUG
from mcphub.web.routes import NEW_BACKEND, RESERVED_SLUGS, _save_backend

PASSWORD = "correct horse battery"


class SpyPlugin(PluginDefaults):
    """Records the instance it was asked about, and never touches anything."""

    id, name, description = "spy", "Spy", "Records what it was asked."
    fields = (
        ConfigField("host", "Host", required=False),
        ConfigField("token", "Token", type="password", secret=True, required=False),
    )

    def __init__(self):
        self.seen: list[BackendInstance] = []

    def build(self, instance):  # pragma: no cover - never mounted here
        raise AssertionError("build must not run for a test")

    async def check(self, instance):
        self.seen.append(instance)
        return CheckResult(True, f"host={instance.get('host')} token={instance.get('token')}")


@pytest.fixture
def hub_app():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://localhost:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    hub = app.state.hub
    hub.registry.register(SpyPlugin())
    for name, admin, can_add in (("boss", 1, 1), ("bystander", 0, 0)):
        hub.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (name, hash_password(PASSWORD), admin, can_add, utcnow()))
    return app


@pytest.fixture
async def clients(hub_app):
    transport = httpx.ASGITransport(app=hub_app)
    async with hub_app.router.lifespan_context(hub_app):
        made = {}
        for who in ("boss", "bystander"):
            client = httpx.AsyncClient(transport=transport, base_url="http://localhost:8080")
            await client.post("/login", data={"username": who, "password": PASSWORD})
            made[who] = client
        yield hub_app.state.hub, made


def typed(**overrides):
    body = {"plugin_id": "spy", "title": "Spy", "slug": "spy", "host": "", "token": ""}
    body.update(overrides)
    return body


# ── testing something that was never saved ────────────────────────────────

async def test_a_backend_that_does_not_exist_yet_can_be_tested(clients):
    """The case the dashboard button could never cover, and the one where
    finding out early is worth the most."""
    hub, who = clients
    response = await who["boss"].post(f"/backends/{NEW_BACKEND}/test", data=typed(host="router"))
    assert response.json()["ok"] is True
    assert "host=router" in response.json()["detail"]


async def test_testing_writes_nothing(clients):
    hub, who = clients
    await who["boss"].post(f"/backends/{NEW_BACKEND}/test", data=typed(host="router"))
    assert added(hub) == []


async def test_what_is_typed_is_what_is_tested(clients):
    hub, who = clients
    await who["boss"].post(f"/backends/{NEW_BACKEND}/test", data=typed(host="typed-in"))
    assert hub.registry.get("spy").seen[-1].get("host") == "typed-in"


# ── testing an edit to something that was saved ───────────────────────────

def save_spy_backend(hub, slug="kept"):
    _save_backend(hub, slug=slug, plugin_id="spy", title="Kept", enabled=False,
                  config={"host": "stored-host"}, secrets={"token": "stored-token"})


async def test_an_edit_is_tested_against_the_edit_not_the_stored_value(clients):
    hub, who = clients
    save_spy_backend(hub)
    await who["boss"].post("/backends/kept/test", data=typed(slug="kept", host="edited-host"))
    assert hub.registry.get("spy").seen[-1].get("host") == "edited-host"


async def test_a_withheld_secret_left_blank_is_merged_back_in(clients):
    """The box renders empty because the value is withheld, so a test that took
    the form at face value would test an empty credential and report a failure
    that is nothing to do with the configuration."""
    hub, who = clients
    save_spy_backend(hub)
    response = await who["boss"].post("/backends/kept/test", data=typed(slug="kept", token=""))
    assert "token=stored-token" in response.json()["detail"]


async def test_a_retyped_secret_beats_the_stored_one(clients):
    hub, who = clients
    save_spy_backend(hub)
    response = await who["boss"].post("/backends/kept/test", data=typed(slug="kept", token="rotated"))
    assert "token=rotated" in response.json()["detail"]


async def test_config_the_form_does_not_show_survives_the_test(clients):
    """A backend carries things no field maps to — where it came from in the
    registry, its cached catalogue. Building the test instance from the form
    alone would drop all of it, exactly as the first save once did."""
    hub, who = clients
    _save_backend(hub, slug="kept", plugin_id="spy", title="Kept", enabled=False,
                  config={"host": "h", "registry_name": "io.github.example/thing"}, secrets={})
    await who["boss"].post("/backends/kept/test", data=typed(slug="kept", host="h"))
    assert hub.registry.get("spy").seen[-1].config["registry_name"] == "io.github.example/thing"


# ── who may ask ───────────────────────────────────────────────────────────

async def test_testing_typed_input_needs_the_right_to_configure_backends(clients):
    """For the proxy plugin a posted command is an arbitrary program to launch,
    so this is the right to configure a backend, not the right to use one."""
    hub, who = clients
    response = await who["bystander"].post(f"/backends/{NEW_BACKEND}/test", data=typed())
    assert response.status_code == 403
    assert hub.registry.get("spy").seen == [], "the plugin was asked anyway"


async def test_a_signed_out_caller_is_refused(hub_app):
    transport = httpx.ASGITransport(app=hub_app)
    async with hub_app.router.lifespan_context(hub_app):
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as anon:
            response = await anon.post(f"/backends/{NEW_BACKEND}/test", data=typed())
    assert response.status_code == 401


async def test_an_unknown_plugin_is_refused(clients):
    hub, who = clients
    response = await who["boss"].post(f"/backends/{NEW_BACKEND}/test",
                                      data=typed(plugin_id="no-such-plugin"))
    assert response.status_code == 404


# ── the dashboard button, which asks a different question ─────────────────

async def test_the_saved_backend_button_still_works(clients):
    """It posts no form, and means "how is the thing that is running"."""
    hub, who = clients
    save_spy_backend(hub)
    response = await who["boss"].post("/backends/kept/test")
    assert response.json()["ok"] is True
    assert "host=stored-host" in response.json()["detail"]


async def test_the_saved_backend_button_reports_a_missing_backend(clients):
    hub, who = clients
    assert (await who["boss"].post("/backends/ghost/test")).status_code == 404


# ── refusing before reaching for the network ──────────────────────────────

async def test_an_incoherent_configuration_is_refused_without_connecting(clients):
    """"Connection refused" is a poor way to learn the URL had no scheme, and a
    launch with no command has nothing to refuse the connection in the first
    place."""
    hub, who = clients
    response = await who["boss"].post(f"/backends/{NEW_BACKEND}/test", data={
        "plugin_id": "mcp-proxy", "title": "T", "slug": "t",
        "connection": "url", "url": "192.168.1.50:8043/mcp", "timeout": "30"})
    body = response.json()
    assert body["ok"] is False
    assert "not a complete URL" in body["detail"]


async def test_a_refusal_names_the_field_in_words_the_form_uses(clients):
    """The result is one line beside a button, with no box to sit under, so it
    has to say which box it is about."""
    hub, who = clients
    response = await who["boss"].post(f"/backends/{NEW_BACKEND}/test", data={
        "plugin_id": "mcp-proxy", "title": "T", "slug": "t",
        "connection": "url", "url": "https://h/mcp", "auth_value": "Bearer s", "timeout": "30"})
    assert "Auth header name:" in response.json()["detail"]


# ── the slug that could never be opened ───────────────────────────────────

def added(hub) -> list:
    """Backends an administrator added, which is every one but the hub's own."""
    return [r for r in hub.backend_rows() if r["slug"] != HUB_SLUG]


def test_new_is_reserved():
    """`/backends/new` is registered ahead of `/backends/{slug}` and both lead
    to the same handler, so a backend named that could be created and then
    never opened again — its settings page would render the create form."""
    assert NEW_BACKEND in RESERVED_SLUGS


async def test_a_backend_cannot_be_named_new(clients):
    hub, who = clients
    response = await who["boss"].post("/backends/new", data={
        "plugin_id": "spy", "title": "T", "slug": NEW_BACKEND, "host": "h"})
    assert response.status_code == 400
    assert "reserved" in response.text
    assert added(hub) == []
