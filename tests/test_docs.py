"""The hub's own documentation, served as a backend.

Documentation about building apps for mcphub is most useful to the thing that
is going to write them, which is generally not a person with a browser open.
These check that it is there, that it is reachable the same way everything else
is, and — the one that matters for a documentation server — that what it says
is still true of the code beside it.
"""

import tempfile
from pathlib import Path

import pytest

from mcphub import roles, storage
from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.hub import DOCS_DIR, ORDER, SLUG, HubPlugin, topics


@pytest.fixture
def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://localhost:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    return create_app(settings).state.hub


@pytest.fixture
def server(hub):
    return hub.registry.get(SLUG).build(
        BackendInstance(slug=SLUG, title=SLUG, plugin_id=SLUG))


async def call(server, name, **arguments) -> str:
    result = await server.call_tool(name, arguments)
    return result.content[0].text


# ── what is there ─────────────────────────────────────────────────────────

def test_every_page_in_the_reading_order_exists():
    """A name in ORDER with no file behind it drops silently out of the list."""
    assert {t.name for t in topics()} >= set(ORDER)


def test_a_page_not_in_the_reading_order_still_appears():
    """Adding a page should not also mean editing a list to mention it."""
    names = [t.name for t in topics()]
    assert len(names) == len(list(DOCS_DIR.glob("*.md")))


def test_each_page_has_a_summary_that_is_a_whole_sentence():
    """The pages are hard-wrapped, so a summary taken from the first *line*
    would be half a sentence, and a topic list of those is worse than useless."""
    for topic in topics():
        assert topic.summary, f"{topic.name} has no summary"
        assert topic.summary.endswith((".", "!", "?")), (
            f"{topic.name}: {topic.summary!r} stops mid-sentence")


# ── and what it answers ───────────────────────────────────────────────────

async def test_listing_names_every_page(server):
    listed = await call(server, "list_topics")
    for topic in topics():
        assert topic.name in listed


async def test_reading_gives_the_whole_page(server):
    assert await call(server, "read_topic", topic="annotations") == \
        (DOCS_DIR / "annotations.md").read_text()


@pytest.mark.parametrize("asked", ["annotations", "Annotations", " annotations ", "annotations.md"])
async def test_a_page_is_found_however_it_is_asked_for(server, asked):
    assert "readOnlyHint" in await call(server, "read_topic", topic=asked)


async def test_an_unknown_page_says_what_there_is(server):
    """Rather than an error a model can only guess its way out of."""
    answer = await call(server, "read_topic", topic="nope")
    assert "annotations" in answer and "storage" in answer


async def test_search_names_the_page_to_read(server):
    found = await call(server, "search_docs", query="MCPHUB_STORAGE")
    assert "[storage]" in found


async def test_search_with_nothing_to_find_says_so(server):
    assert "Nothing mentions" in await call(server, "search_docs", query="zzzznotathing")


# ── reachable like everything else ────────────────────────────────────────

DOC_TOOLS = ("list_topics", "read_topic", "search_docs")


async def test_every_documentation_tool_is_read_only(server):
    """So the documentation is readable at every level, including viewer —
    which is also the hub's own smallest end-to-end use of `annotations`."""
    tools = {t.name: t for t in await server.list_tools()}
    for name in DOC_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
        assert roles.allows(roles.VIEWER, tools[name]), name


async def test_each_page_is_a_resource_a_client_can_attach(server):
    """A `docs://{name}` template would not be listed, so a client offering a
    menu of what it can attach would show nothing."""
    listed = {str(r.uri) for r in await server.list_resources()}
    assert listed == {f"docs://{t.name}" for t in topics()}


async def test_it_says_what_it_found(hub):
    result = await hub.registry.get(SLUG).check(
        BackendInstance(slug=SLUG, title="d", plugin_id=SLUG))
    assert result.ok and "pages" in result.detail


def test_a_new_hub_comes_with_it_mounted(hub):
    hub.ensure_own_backend()
    row = hub.backend_row(SLUG)
    assert row is not None and row["enabled"]


def test_it_comes_back_if_the_row_goes(hub):
    """It is part of the hub, not something an administrator added, so it is
    not theirs to delete — and a row that can be deleted will be."""
    hub.ensure_own_backend()
    hub.db.execute("DELETE FROM backends WHERE slug = ?", (SLUG,))
    hub.ensure_own_backend()
    assert hub.backend_row(SLUG) is not None


def test_disabling_it_sticks(hub):
    """What is theirs is whether it is exposed."""
    hub.ensure_own_backend()
    hub.db.execute("UPDATE backends SET enabled = 0 WHERE slug = ?", (SLUG,))
    hub.ensure_own_backend()
    assert not hub.backend_row(SLUG)["enabled"]


def test_an_earlier_docs_backend_is_carried_over_rather_than_replaced(hub):
    """Renaming keeps its grants and pins; deleting and recreating would drop
    them without saying so."""
    from mcphub.db import utcnow

    hub.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, "
        "created_at, updated_at) VALUES ('docs', 'mcphub-docs', 'docs', 1, '{}', ?, ?)",
        (utcnow(), utcnow()))
    before = hub.backend_row("docs")["id"]
    hub.db.execute("INSERT INTO users (username, password_hash, created_at) "
                   "VALUES ('reader', 'x', ?)", (utcnow(),))
    uid = hub.db.one("SELECT id FROM users WHERE username = 'reader'")["id"]
    hub.db.execute("INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
                   "VALUES (?, ?, 'viewer', ?)", (uid, before, utcnow()))

    hub.ensure_own_backend()
    after = hub.backend_row(SLUG)
    assert after is not None and after["id"] == before
    assert after["plugin_id"] == SLUG
    assert hub.backend_row("docs") is None
    assert hub.db.one("SELECT role FROM backend_grants WHERE backend_id = ?",
                      (before,))["role"] == "viewer"


# ── still true of the code beside it ──────────────────────────────────────

def page(name: str) -> str:
    return (DOCS_DIR / f"{name}.md").read_text()


def flat(name: str) -> str:
    """The page with its hard wrapping taken out, so a phrase that happens to
    straddle a line break still reads as one phrase."""
    return " ".join(page(name).split())


def test_the_storage_page_names_the_variable_that_is_actually_set():
    assert storage.ENV_VAR in flat("storage")
    assert f"${storage.ENV_VAR}/" in flat("storage"), "the expansion example must be the real name"


def test_the_storage_page_names_the_real_config_key():
    assert storage.PER_VERSION in flat("storage")


def test_the_storage_page_shows_the_hook_a_plugin_actually_overrides():
    """The page tells a plugin author to write one method. It has to be the
    method the hub calls, or the instruction produces a plugin with no storage
    and nothing saying why."""
    from mcphub.plugins.base import PluginDefaults

    assert hasattr(PluginDefaults, "uses_storage")
    assert "def uses_storage(self, instance)" in flat("storage")


def test_the_storage_page_says_a_proxied_server_gets_none():
    """Which is the behaviour, and the thing someone will otherwise report as
    a bug when the section is missing from their settings page."""
    from mcphub.plugins.builtin.mcpproxy import PLUGIN as PROXY
    from mcphub.plugins.base import BackendInstance

    instance = BackendInstance(slug="s", title="t", plugin_id=PROXY.id)
    assert PROXY.uses_storage(instance) is False
    assert "proxies gets none" in flat("storage")


def test_the_levels_page_lists_the_levels_that_exist():
    text = flat("levels")
    for level in roles.LEVELS:
        assert f"`{level}`" in text


def test_the_web_interface_page_names_the_headers_that_are_sent():
    import inspect

    from mcphub.web import routes

    sent = {h for h in ("x-mcphub-user", "x-mcphub-admin", "x-mcphub-role")
            if h in inspect.getsource(routes.build)}
    assert sent, "the identity headers moved; this test cannot see them any more"
    text = flat("web-interface").lower()
    for header in sent:
        assert header in text
