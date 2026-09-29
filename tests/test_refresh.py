"""Re-reading what an upstream offers.

A launched server resolves its package again every time it starts, so
remounting is what picks up a new release. The cached tool list has to be
re-read at the same time — otherwise a server gains tools and the hub goes on
serving the list it read when the backend was first saved.
"""

import json

import pytest

from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.mcpproxy import PLUGIN
from mcphub.web.routes import _config_value

CATALOG = [
    {"name": "get_current_time", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "convert_time", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
]


def instance(**config) -> BackendInstance:
    return BackendInstance(slug="s", title="t", plugin_id="mcp-proxy", config=config)


def test_tool_names_reads_the_cached_catalogue():
    assert PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG))) == {
        "get_current_time", "convert_time"}


def test_tool_names_of_an_empty_backend():
    assert PLUGIN.tool_names(instance()) == set()


def test_tool_names_survives_a_corrupt_catalogue():
    """A bad cache must not stop a refresh from being able to fix it."""
    assert PLUGIN.tool_names(instance(tool_catalog="{not json")) == set()


def test_added_and_removed_are_a_set_difference():
    was = PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG[:1])))
    now = PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG)))
    assert sorted(now - was) == ["convert_time"]
    assert sorted(was - now) == []


@pytest.mark.parametrize("stored,expected", [
    ({"upstream_version": "1.30.0"}, "1.30.0"),
    ({}, ""),
    ({"upstream_version": None}, ""),
])
def test_config_value(stored, expected):
    row = {"config_json": json.dumps(stored)}
    assert _config_value(row, "upstream_version") == expected


def test_config_value_survives_a_corrupt_row():
    assert _config_value({"config_json": "{not json"}, "upstream_version") == ""


def test_allowlist_pruning_keeps_tools_that_still_exist():
    """The rule the refresh applies: drop only what the upstream no longer has."""
    selected = ["get_current_time", "convert_time", "gone_away"]
    available = {"get_current_time", "convert_time"}
    assert [t for t in selected if t in available] == ["get_current_time", "convert_time"]


# ── what Update leaves running ────────────────────────────────────────────
#
# Adoption used to happen *after* the remount: the widened selection reached
# the database and the endpoint went on being built from the old one. Update
# then reported a tool as exposed while the server did not serve it, and only
# an unrelated manual save ever put that right.

import tempfile
from pathlib import Path

import httpx2 as httpx

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import CheckResult, ConfigField, Option, PluginDefaults
from mcphub.plugins.builtin.mcpproxy import ADOPT_KEY, ALLOW_KEY

BASE = "http://127.0.0.1:8080"
PASSWORD = "correct horse battery"


class Growing(PluginDefaults):
    """An upstream whose tool list the test can change under the hub's feet."""

    id, name, description = "growing", "Growing", "d"
    fields = (
        ConfigField(ALLOW_KEY, "Tools to expose", type="multiselect", required=False),
        ConfigField(ADOPT_KEY, "Expose new tools automatically", type="bool",
                    default=False, required=False),
    )

    def __init__(self, offers: str = "look"):
        self.offers = offers

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(instance.title)

    def tool_names(self, instance) -> set[str]:
        return {n for n in str(instance.config.get("catalog", "") or "").split(",") if n}

    async def options(self, instance, key):
        return [Option(value=n, label=n) for n in sorted(self.tool_names(instance))]

    async def on_save(self, instance):
        return {"catalog": self.offers}

    async def check(self, instance):
        return CheckResult(True, "ok")


@pytest.fixture
async def running():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Growing())
    state.db.execute(
        "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
        "VALUES ('boss', ?, 1, 1, ?)", (hash_password(PASSWORD), utcnow()))
    async with app.router.lifespan_context(app):
        browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                    follow_redirects=False)
        await browser.post("/login", data={"username": "boss", "password": PASSWORD})
        yield state, browser, state.registry.get("growing")


def form(**overrides):
    """A multiselect posts its key once per choice, so `tools` takes a list."""
    return {"plugin_id": "growing", "title": "Grower", "slug": "grower",
            "enabled": "on", **overrides}


def mounted_selection(state) -> list[str] | None:
    """What the endpoint that is actually running was built from."""
    return state.mounts._mounted["grower"].instance.config.get(ALLOW_KEY)


def stored_selection(state) -> list[str] | None:
    return state.instance_from_row(state.backend_row("grower")).config.get(ALLOW_KEY)


async def settled(browser, **overrides):
    await browser.post("/backends/new", data=form(**overrides))


async def test_an_adopted_tool_reaches_the_running_endpoint(running):
    """The whole point of the setting. Storing it and serving something else
    is the one outcome that looks like it worked."""
    state, browser, upstream = running
    await settled(browser, tools="look", tools_adopt_new="on")

    upstream.offers = "look,change"
    await browser.post("/backends/grower/refresh")
    assert sorted(mounted_selection(state)) == ["change", "look"]


async def test_what_is_running_is_what_was_stored(running):
    """The invariant the ordering broke: a save the remount never carried."""
    state, browser, upstream = running
    await settled(browser, tools="look", tools_adopt_new="on")

    upstream.offers = "look,change"
    await browser.post("/backends/grower/refresh")
    assert sorted(mounted_selection(state)) == sorted(stored_selection(state))


async def test_a_tool_the_upstream_lost_leaves_the_running_endpoint(running):
    state, browser, upstream = running
    upstream.offers = "look,change"
    await settled(browser, tools=["look", "change"])

    upstream.offers = "look"
    await browser.post("/backends/grower/refresh")
    assert mounted_selection(state) == ["look"]
    assert sorted(mounted_selection(state)) == sorted(stored_selection(state))


async def test_nothing_is_adopted_when_the_setting_is_off(running):
    state, browser, upstream = running
    await settled(browser, tools="look")

    upstream.offers = "look,change"
    answer = await browser.post("/backends/grower/refresh")
    assert mounted_selection(state) == ["look"]
    assert "and exposed" not in answer.json()["detail"]


async def test_a_new_tool_is_still_reported_when_it_is_not_adopted(running):
    """Not exposing it is the choice; not mentioning it would be a surprise."""
    state, browser, upstream = running
    await settled(browser, tools="look")

    upstream.offers = "look,change"
    assert "change" in (await browser.post("/backends/grower/refresh")).json()["detail"]


async def test_an_empty_selection_is_not_described_as_adopting(running):
    """It already exposes everything, so there is nothing to adopt into — and
    the reply used to say "and exposed" on the strength of the setting alone."""
    state, browser, upstream = running
    await settled(browser, tools_adopt_new="on")

    upstream.offers = "look,change"
    answer = await browser.post("/backends/grower/refresh")
    assert "and exposed" not in answer.json()["detail"]
    assert not stored_selection(state)


async def test_a_refresh_that_changes_nothing_says_so(running):
    state, browser, upstream = running
    await settled(browser, tools="look", tools_adopt_new="on")
    assert "nothing changed" in (await browser.post("/backends/grower/refresh")).json()["detail"]
