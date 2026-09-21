"""The hub's own backend: built in, reserved, and able to deploy things.

Two separate claims are checked here. That `mcphub` cannot be taken, renamed or
deleted — a backend that is part of the hub should behave like it. And that its
management tools ask both questions they have to ask: whether the account may
configure backends at all, and what this connector's level is. They are
different questions, and a tool that only asks one of them is a hole.
"""

import contextlib
import json
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from mcphub import roles, storage
from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, PluginDefaults
from mcphub.plugins.builtin.hub import SLUG

PASSWORD = "correct horse battery"


class Simple(PluginDefaults):
    """Something that always builds, so enabling a backend can be tested."""

    id, name, description, fields = "simple", "Simple", "d", ()

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(instance.title)

    async def check(self, instance):  # pragma: no cover
        from mcphub.plugins.base import CheckResult

        return CheckResult(True, "ok")


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://127.0.0.1:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Simple())
    for name, admin, adds in (("boss", 1, 1), ("helper", 0, 1), ("reader", 0, 0)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (name, hash_password(PASSWORD), admin, adds, utcnow()))
    # The real thing, so the tools act on a hub whose mounts exist.
    async with app.router.lifespan_context(app):
        yield state


@pytest.fixture
def server(hub):
    return hub.registry.get(SLUG).build(
        BackendInstance(slug=SLUG, title=SLUG, plugin_id=SLUG))


@contextlib.contextmanager
def signed_in(username: str):
    """The caller the tools see, which the hub's own middleware normally sets."""
    token = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="t", client_id="c", scopes=["mcp:use"], subject=username)))
    try:
        yield
    finally:
        auth_context_var.reset(token)


async def call(server, name, **arguments) -> str:
    result = await server.call_tool(name, arguments)
    return result.content[0].text


def add_backend(hub, slug: str, enabled: bool = False) -> None:
    hub.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, "
        "created_at, updated_at) VALUES (?, 'simple', ?, ?, '{}', ?, ?)",
        (slug, slug.title(), int(enabled), utcnow(), utcnow()))


# ── built in, and staying that way ────────────────────────────────────────

def test_a_new_hub_has_it(hub):
    row = hub.backend_row(SLUG)
    assert row is not None and row["plugin_id"] == SLUG and row["enabled"]


def test_the_name_is_reserved(hub):
    from mcphub.web.routes import RESERVED_SLUGS

    assert SLUG in RESERVED_SLUGS


def test_it_is_not_offered_as_something_to_add(hub):
    """It is already here; listing it would offer a second copy of the hub."""
    import inspect

    from mcphub.web import routes

    assert "p.id != HUB_SLUG" in inspect.getsource(routes.build)


async def test_it_will_not_remove_itself(hub, server):
    with signed_in("boss"), pytest.raises(ToolError) as raised:
        await call(server, "remove_backend", slug=SLUG)
    assert "cannot be removed" in str(raised.value)
    assert hub.backend_row(SLUG) is not None


# ── who may do what ───────────────────────────────────────────────────────

async def test_reading_needs_no_special_right(hub, server):
    add_backend(hub, "router")
    with signed_in("boss"):
        assert "router" in await call(server, "list_backends")


async def test_an_account_sees_only_what_it_was_granted(hub, server):
    add_backend(hub, "router")
    add_backend(hub, "unraid")
    reader = hub.db.one("SELECT id FROM users WHERE username = 'reader'")["id"]
    hub.db.execute(
        "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
        "SELECT ?, id, 'viewer', ? FROM backends WHERE slug = 'router'", (reader, utcnow()))
    with signed_in("reader"):
        listed = await call(server, "list_backends")
    assert "router" in listed and "unraid" not in listed


@pytest.mark.parametrize("tool,arguments", [
    ("deploy_app", {"slug": "new-one", "url": "http://x/mcp"}),
    ("set_backend_enabled", {"slug": "router", "enabled": False}),
    ("remove_backend", {"slug": "router"}),
    ("search_registry", {"query": "anything"}),
])
async def test_configuring_needs_the_right_to_configure(hub, server, tool, arguments):
    """Separate from the level. An account that may not touch backends in the
    web UI must not be able to touch them through a connector either."""
    add_backend(hub, "router")
    with signed_in("reader"), pytest.raises(ToolError) as raised:
        await call(server, tool, **arguments)
    assert "may not configure backends" in str(raised.value)


async def test_an_account_that_no_longer_exists_is_refused(hub, server):
    with signed_in("ghost"), pytest.raises(ToolError) as raised:
        await call(server, "list_backends")
    assert "no longer exists" in str(raised.value)


async def test_an_unauthenticated_call_decides_nothing(hub, server):
    with pytest.raises(ToolError) as raised:
        await call(server, "list_backends")
    assert "no account" in str(raised.value)


# ── the levels these tools carry ──────────────────────────────────────────

EXPECTED = {
    "list_topics": roles.VIEWER, "read_topic": roles.VIEWER, "search_docs": roles.VIEWER,
    "list_backends": roles.VIEWER, "describe_backend": roles.VIEWER,
    "search_registry": roles.VIEWER,
    "deploy_app": roles.USER, "set_backend_enabled": roles.USER,
    "remove_backend": roles.ADMIN,
}


async def test_every_tool_carries_the_level_it_is_documented_with(server):
    """The table on the `mcphub-backend` page is a promise about annotations,
    and annotations are what the hub actually enforces."""
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == set(EXPECTED), "a tool changed; the documentation has to follow"
    for name, lowest in EXPECTED.items():
        allowed = [level for level in roles.LEVELS if roles.allows(level, tools[name])]
        assert allowed[0] == lowest, f"{name} is reachable from {allowed[0]}, not {lowest}"


def test_the_documented_table_matches(server):
    from mcphub.plugins.builtin.hub import DOCS_DIR

    page = " ".join((DOCS_DIR / "mcphub-backend.md").read_text().split())
    for name, level in EXPECTED.items():
        if name.startswith(("list_topics", "read_topic", "search_docs")):
            continue
        assert f"`{name}` | {level} |" in page, f"{name} is not in the table at {level}"


# ── deploying ─────────────────────────────────────────────────────────────

async def test_deploying_from_a_url_creates_a_disabled_backend(hub, server):
    """Disabled on purpose: the tools come from outside this hub and should be
    looked at before they attach to an account."""
    with signed_in("helper"):
        answer = json.loads(await call(server, "deploy_app", slug="dictionary",
                                       url="http://dictionary:8000/mcp"))
    assert answer["enabled"] is False
    row = hub.backend_row("dictionary")
    assert row is not None and not row["enabled"]
    assert json.loads(row["config_json"])["url"] == "http://dictionary:8000/mcp"


async def test_a_proxied_backend_is_given_no_storage(hub, server):
    """A server the hub merely proxies keeps its data wherever it already keeps
    it, so the hub does not name a path it could not hand over anyway."""
    with signed_in("helper"):
        answer = json.loads(await call(server, "deploy_app", slug="dictionary",
                                       url="http://d:8000/mcp"))
    assert answer["storage"] is None
    assert not storage.path_for(hub.settings.data_dir, "dictionary").exists()


@pytest.mark.parametrize("slug,reason", [
    (SLUG, "reserved"), ("mcp", "reserved"), ("no pe", "URL name"), ("a", "URL name"),
])
async def test_a_name_it_cannot_have_is_refused(hub, server, slug, reason):
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "deploy_app", slug=slug, url="http://x/mcp")
    assert reason in str(raised.value)


async def test_a_name_already_taken_is_refused(hub, server):
    add_backend(hub, "router")
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "deploy_app", slug="router", url="http://x/mcp")
    assert "already exists" in str(raised.value)


@pytest.mark.parametrize("arguments", [
    {},                                                  # neither
    {"registry_name": "a/b", "url": "http://x/mcp"},     # both
])
async def test_it_must_be_told_exactly_one_source(hub, server, arguments):
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "deploy_app", slug="dictionary", **arguments)
    assert "exactly one" in str(raised.value)


async def test_a_url_that_is_not_one_is_refused(hub, server):
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "deploy_app", slug="dictionary", url="dictionary:8000")
    assert "http" in str(raised.value)


async def test_values_it_is_given_are_stored_encrypted(hub, server):
    """Which of them are sensitive is the server's claim, and a wrong claim
    should not put a token in a plaintext column."""
    with signed_in("helper"):
        await call(server, "deploy_app", slug="dictionary", url="http://d/mcp",
                   environment={"API_TOKEN": "s3cret"})
    row = hub.backend_row("dictionary")
    assert "s3cret" not in row["config_json"]
    assert hub.instance_from_row(row).secrets["env_API_TOKEN"] == "s3cret"


# ── and the rest of the life cycle ────────────────────────────────────────

async def test_describing_names_the_storage_and_the_grants_but_not_the_secrets(hub, server):
    with signed_in("helper"):
        await call(server, "deploy_app", slug="dictionary", url="http://d/mcp",
                   environment={"API_TOKEN": "s3cret"})
        described = json.loads(await call(server, "describe_backend", slug="dictionary"))
    assert described["secrets_set"] == ["env_API_TOKEN"]
    assert "s3cret" not in json.dumps(described)


async def test_describing_something_not_granted_is_refused(hub, server):
    add_backend(hub, "router")
    with signed_in("reader"), pytest.raises(ToolError) as raised:
        await call(server, "describe_backend", slug="router")
    assert "not been granted" in str(raised.value)


async def test_removing_keeps_the_files(hub, server):
    """Unmounting is reversible and a dropped database is not."""
    with signed_in("helper"):
        await call(server, "deploy_app", slug="dictionary", url="http://d/mcp")
    kept = storage.ensure(hub.settings.data_dir, "dictionary")
    (kept / "words.db").write_text("kept")

    with signed_in("helper"):
        await call(server, "remove_backend", slug="dictionary")
    assert hub.backend_row("dictionary") is None
    assert (kept / "words.db").read_text() == "kept"


async def test_removing_something_that_is_not_there_says_so(hub, server):
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "remove_backend", slug="nope")
    assert "No backend called" in str(raised.value)


async def test_enabling_brings_the_endpoint_up(hub, server):
    add_backend(hub, "router")
    assert "router" not in {m.slug for m in hub.mounts.active()}

    with signed_in("helper"):
        said = await call(server, "set_backend_enabled", slug="router", enabled=True)
    assert "router" in {m.slug for m in hub.mounts.active()}
    assert "/mcp/router" in said


async def test_disabling_takes_it_down_but_keeps_the_settings(hub, server):
    add_backend(hub, "router")
    with signed_in("helper"):
        await call(server, "set_backend_enabled", slug="router", enabled=True)
        await call(server, "set_backend_enabled", slug="router", enabled=False)
    assert "router" not in {m.slug for m in hub.mounts.active()}
    assert hub.backend_row("router") is not None


async def test_enabling_something_that_cannot_start_says_so(hub, server):
    """And leaves it enabled, which is the state that matches the settings —
    silently disabling it again would hide the fault behind a tick box."""
    hub.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, "
        "created_at, updated_at) VALUES ('broken', 'nonexistent', 'B', 0, '{}', ?, ?)",
        (utcnow(), utcnow()))
    with signed_in("helper"), pytest.raises(ToolError) as raised:
        await call(server, "set_backend_enabled", slug="broken", enabled=True)
    assert "did not start" in str(raised.value)


async def test_it_reports_what_is_live(hub, server):
    add_backend(hub, "router")
    with signed_in("helper"):
        await call(server, "set_backend_enabled", slug="router", enabled=True)
        listed = await call(server, "list_backends")
    assert "(live)" in [line for line in listed.splitlines() if line.startswith("router")][0]


async def test_a_deployed_backend_can_be_granted_and_then_used(hub, server):
    """The whole loop in one test: deploy, enable, grant, and it answers.

    Each step is checked elsewhere; what this pins is that they compose — that
    something created through a connector is an ordinary backend afterwards,
    with nothing left in a half-state.
    """
    add_backend(hub, "router")
    with signed_in("helper"):
        await call(server, "set_backend_enabled", slug="router", enabled=True)

    reader = hub.db.one("SELECT id FROM users WHERE username = 'reader'")["id"]
    hub.db.execute(
        "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
        "SELECT ?, id, 'viewer', ? FROM backends WHERE slug = 'router'", (reader, utcnow()))

    with signed_in("reader"):
        listed = await call(server, "list_backends")
        described = json.loads(await call(server, "describe_backend", slug="router"))
    assert "router" in listed
    assert described["granted_to"] == [{"account": "reader", "level": roles.VIEWER}]


async def test_an_app_granted_this_backend_can_read_but_not_configure(hub, server):
    """An app identity is an ordinary account, so it arrives with no right to
    configure backends — and granting it `admin` here does not add one.

    The two checks being independent is the whole point of having both: one is
    about the account, the other about what this connector was granted.
    """
    add_backend(hub, "router")
    hub.apps.set_grants("dictionary", {SLUG: roles.ADMIN})

    with signed_in("app:dictionary"):
        listed = await call(server, "list_backends")
        with pytest.raises(ToolError) as raised:
            await call(server, "deploy_app", slug="another", url="http://x/mcp")
    assert SLUG in listed, "it was granted this backend, so it reads this backend"
    assert "router" not in listed, "and nothing it was not granted"
    assert "may not configure backends" in str(raised.value)


async def test_an_unreachable_server_is_said_to_be_unreachable(hub, server):
    """An empty tool list means two very different things — "this server has
    nothing" and "nobody has looked yet" — and the second is normal when the
    app being deployed is not running yet."""
    with signed_in("helper"):
        answer = json.loads(await call(server, "deploy_app", slug="dictionary",
                                       url="http://nothing-here:9/mcp"))
    assert answer["tools_found"] == []
    assert "could not be reached" in answer["note"]
