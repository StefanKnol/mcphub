"""Adding a backend in one sitting, and keeping up with what it offers.

Adding one used to take two visits. The tools cannot be chosen until something
has connected and read them, so the form showed "save this first, then come
back" — and saving sent you to the dashboard, from which you walked straight
back into the same settings page. Test now reads them where you are, and saving
leaves you on the page rather than somewhere you have to navigate out of.

The other half is what happens later: a server that gains a tool leaves it
hidden, because a narrowed list is narrowed on purpose. `tools_adopt_new` says
otherwise, for a server you would rather track.
"""

import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, CheckResult, ConfigField, Option, PluginDefaults
from mcphub.plugins.builtin.mcpproxy import ADOPT_KEY, ALLOW_KEY
from mcphub.web.routes import adopt_new_tools

BASE = "http://127.0.0.1:8080"
PASSWORD = "correct horse battery"


class Countable(PluginDefaults):
    """A plugin whose tool list is whatever its `tools_seen` config says.

    Standing in for an upstream that gains and loses tools, without a network.
    """

    id, name, description = "countable", "Countable", "d"
    fields = (
        ConfigField("host", "Host", required=False),
        ConfigField("seen", "Tools it has", required=False),
        ConfigField(ALLOW_KEY, "Tools to expose", type="multiselect", required=False),
        ConfigField(ADOPT_KEY, "Expose new tools automatically", type="bool",
                    default=False, required=False),
    )

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(instance.title)

    def tool_names(self, instance) -> set[str]:
        raw = str(instance.config.get("catalog", "") or "")
        return {n for n in raw.split(",") if n}

    async def options(self, instance, key):
        if key != ALLOW_KEY:
            return ()
        return [Option(value=n, label=n, help=f"does {n}")
                for n in sorted(self.tool_names(instance))]

    async def on_save(self, instance):
        """What the upstream turns out to have, cached the way the proxy does."""
        return {"catalog": str(instance.config.get("seen", "") or "")}

    async def check(self, instance):
        return CheckResult(True, f"Connected, {len(self.tool_names(instance))} tools")


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Countable())
    state.db.execute(
        "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
        "VALUES ('boss', ?, 1, 1, ?)", (hash_password(PASSWORD), utcnow()))
    async with app.router.lifespan_context(app):
        browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                    follow_redirects=False)
        await browser.post("/login", data={"username": "boss", "password": PASSWORD})
        yield state, browser


def typed(**overrides) -> dict[str, str]:
    return {"plugin_id": "countable", "title": "Timeserver", "slug": "timeserver",
            "host": "10.0.0.9", "seen": "look,change", **overrides}


# ── one sitting ───────────────────────────────────────────────────────────

async def test_a_new_form_says_to_test_rather_than_to_save_and_come_back(hub):
    _, browser = hub
    page = await browser.get("/backends/new?plugin=countable")
    assert "Press <strong>Test</strong>" in page.text


async def test_testing_reports_the_tools_it_found(hub):
    """The choice cannot be offered until something has connected. Connecting
    is what Test does, so it may as well come back with them."""
    _, browser = hub
    answer = (await browser.post("/backends/new/test", data=typed())).json()
    assert answer["ok"]
    assert [o["value"] for o in answer["options"][ALLOW_KEY]] == ["change", "look"]


async def test_a_failed_test_offers_no_choices(hub):
    _, browser = hub
    answer = (await browser.post("/backends/new/test",
                                 data=typed(plugin_id="nonexistent"))).json()
    assert not answer["ok"]
    assert not answer.get("options")


async def test_testing_saves_nothing(hub):
    """It reads the upstream to fill the form; the form is still unsaved."""
    state, browser = hub
    await browser.post("/backends/new/test", data=typed())
    assert state.backend_row("timeserver") is None


async def test_saving_a_new_backend_stays_on_its_settings(hub):
    state, browser = hub
    done = await browser.post("/backends/new", data=typed(enabled="on"))
    assert done.status_code == 303
    assert done.headers["location"].startswith("/backends/timeserver?saved=1")
    assert "new=1" in done.headers["location"]


async def test_the_page_it_lands_on_says_what_is_left_to_do(hub):
    _, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on"))
    page = await browser.get("/backends/timeserver?saved=1&new=1")
    assert "Added and started" in page.text
    assert "/backends/timeserver/app" in page.text, "the other half of the setup"


async def test_the_tools_are_there_when_it_lands(hub):
    """The whole point of staying: the list that could not be offered before
    the save is the first thing on the page after it."""
    _, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on"))
    page = await browser.get("/backends/timeserver")
    assert 'value="look"' in page.text and 'value="change"' in page.text


async def test_saving_an_existing_backend_also_stays(hub):
    _, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on"))
    done = await browser.post("/backends/timeserver",
                              data=typed(title="Renamed", enabled="on"))
    assert done.headers["location"] == "/backends/timeserver?saved=1"
    assert "new=1" not in done.headers["location"], "it was not added just now"


# ── keeping up with what it offers ────────────────────────────────────────

async def test_a_first_save_respects_what_was_ticked(hub):
    """With no previous catalogue every tool is new, and adopting on that
    reading would have overridden the choice just made."""
    state, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on", tools="look",
                                                   tools_adopt_new="on"))
    stored = state.instance_from_row(state.backend_row("timeserver"))
    assert stored.config[ALLOW_KEY] == ["look"]


async def test_a_tool_that_appears_later_is_adopted_when_asked(hub):
    state, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on", seen="look",
                                                   tools="look", tools_adopt_new="on"))
    await browser.post("/backends/timeserver", data=typed(enabled="on", seen="look,change",
                                                          tools="look", tools_adopt_new="on"))
    stored = state.instance_from_row(state.backend_row("timeserver"))
    assert sorted(stored.config[ALLOW_KEY]) == ["change", "look"]


async def test_it_stays_hidden_when_not_asked(hub):
    """A narrowed list is usually narrowed on purpose, and a server that
    quietly gains reach is not what anyone asked for."""
    state, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on", seen="look", tools="look"))
    await browser.post("/backends/timeserver",
                       data=typed(enabled="on", seen="look,change", tools="look"))
    stored = state.instance_from_row(state.backend_row("timeserver"))
    assert stored.config[ALLOW_KEY] == ["look"]


async def test_a_refresh_adopts_too(hub):
    """The other place a catalogue changes under a selection."""
    state, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on", seen="look",
                                                   tools="look", tools_adopt_new="on"))
    import json

    row = state.backend_row("timeserver")
    config = json.loads(row["config_json"]) | {"seen": "look,change"}
    state.db.execute("UPDATE backends SET config_json = ? WHERE id = ?",
                     (json.dumps(config), row["id"]))

    answer = (await browser.post("/backends/timeserver/refresh")).json()
    assert answer["ok"], answer
    assert "and exposed" in answer["detail"]
    stored = state.instance_from_row(state.backend_row("timeserver"))
    assert sorted(stored.config[ALLOW_KEY]) == ["change", "look"]


# ── the rule on its own ───────────────────────────────────────────────────

@pytest.mark.parametrize("config,before,after,expected", [
    # Nothing selected already means everything; there is nothing to add to.
    ({ADOPT_KEY: True, ALLOW_KEY: []}, {"a"}, {"a", "b"}, []),
    ({ADOPT_KEY: False, ALLOW_KEY: ["a"]}, {"a"}, {"a", "b"}, ["a"]),
    ({ADOPT_KEY: True, ALLOW_KEY: ["a"]}, {"a"}, {"a", "b"}, ["a", "b"]),
    ({ADOPT_KEY: True, ALLOW_KEY: ["a"]}, set(), {"a", "b"}, ["a"]),
    ({ADOPT_KEY: True, ALLOW_KEY: ["a"]}, {"a", "b"}, {"a"}, ["a"]),
])
def test_when_a_new_tool_joins_the_selection(config, before, after, expected):
    adopt_new_tools(config, before, after)
    assert config[ALLOW_KEY] == expected


def test_nothing_is_added_twice():
    config = {ADOPT_KEY: True, ALLOW_KEY: ["a", "b"]}
    adopt_new_tools(config, {"a"}, {"a", "b"})
    assert config[ALLOW_KEY] == ["a", "b"]


# ── which fields a test depends on ────────────────────────────────────────

def test_a_field_is_assumed_to_matter_unless_it_says_otherwise():
    """The safe way round. A field that quietly leaves a stale pass behind is
    how someone saves a URL that was never tried, believing it was."""
    assert ConfigField("host", "Host").probes is True


def test_choosing_among_what_a_connection_returned_cannot_invalidate_it():
    assert ConfigField("tools", "Tools", type="multiselect").probes is False


@pytest.mark.parametrize("declared,expected", [(True, True), (False, False)])
def test_a_plugin_can_say_either_way(declared, expected):
    assert ConfigField("k", "K", affects_connection=declared).probes is expected


def test_the_proxys_connection_settings_all_count():
    from mcphub.plugins.builtin.mcpproxy import PLUGIN

    probing = {f.key for f in PLUGIN.fields if f.page == "mcp" and f.probes}
    assert {"connection", "command", "env", "url", "auth_header", "auth_value",
            "verify_tls", "timeout"} <= probing


def test_and_choosing_tools_does_not():
    from mcphub.plugins.builtin.mcpproxy import PLUGIN

    for key in (ALLOW_KEY, ADOPT_KEY):
        assert next(f for f in PLUGIN.fields if f.key == key).probes is False


async def test_the_form_marks_them_for_the_page(hub):
    """The button goes back to Test off these markers, so a field that stops
    carrying one stops invalidating anything and nothing says so."""
    _, browser = hub
    page = (await browser.get("/backends/new?plugin=countable")).text
    assert 'data-field-key="host"' in page
    connects = page.split('data-field-key="host"')[1].split(">")[0]
    assert "data-connects" in connects

    tools = page.split(f'data-field-key="{ALLOW_KEY}"')[1].split(">")[0]
    assert "data-connects" not in tools


async def test_a_new_backend_starts_on_test(hub):
    _, browser = hub
    page = (await browser.get("/backends/new?plugin=countable")).text
    assert ">Test</button>" in page
    assert "Save untested" in page


async def test_one_that_is_already_running_starts_on_save(hub):
    """Its connection is known good; asking for a test to change its title
    would be asking for a round trip to prove something nothing touched."""
    _, browser = hub
    await browser.post("/backends/new", data=typed(enabled="on"))
    page = (await browser.get("/backends/timeserver")).text
    assert '>Save</button>' in page


async def test_saving_untested_is_always_available(hub):
    """A form that can only be saved by connecting is a trap when the thing at
    the other end is simply not up yet."""
    state, browser = hub
    done = await browser.post("/backends/new", data=typed(slug="offline", host=""))
    assert done.status_code == 303
    assert state.backend_row("offline") is not None
