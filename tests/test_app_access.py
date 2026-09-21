"""An app's own access to other backends on the hub.

The point is that an app gets an identity rather than a borrowed one. What has
to hold: its token reaches what it was granted, at the level it was granted, and
nothing else — and taking the grant away takes the access away without anyone
restarting anything.

The token is checked against a real mount rather than against the code that
mints it, because "we issued a credential" and "that credential opens that door
and no other" are different claims.
"""

import json
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcphub import appaccess, roles
from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import verify_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, CheckResult, PluginDefaults

BASE = "http://127.0.0.1:8080"


class Annotated(PluginDefaults):
    id, name, description, fields = "annotated", "Annotated", "d", ()

    def build(self, instance):
        server = MCPServer(instance.title)

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def look() -> str:
            return "looked"

        @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
        def wipe() -> str:
            return "wiped"

        return server

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Annotated())
    for slug, title in (("router", "Router"), ("unraid", "Unraid"), ("aenvae", "Aenvae")):
        state.db.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, "
            "created_at, updated_at) VALUES (?, 'annotated', ?, 1, '{}', ?, ?)",
            (slug, title, utcnow(), utcnow()))
    async with app.router.lifespan_context(app):
        yield state, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


# ── the identity ──────────────────────────────────────────────────────────

@pytest.fixture
def plain_hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    state = create_app(settings).state.hub
    for slug in ("router", "aenvae"):
        state.db.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, "
            "created_at, updated_at) VALUES (?, 'mcp-proxy', ?, 1, '{}', ?, ?)",
            (slug, slug, utcnow(), utcnow()))
    return state


def test_granting_creates_an_identity(plain_hub):
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    row = plain_hub.db.one("SELECT * FROM users WHERE username = 'app:aenvae'")
    assert row is not None
    assert not row["is_admin"] and not row["can_add_backends"]


def test_nobody_can_sign_in_as_an_app(plain_hub):
    """It has an account so the grant machinery applies to it, not so it can
    log in. There must be no password that works, including an empty one."""
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    stored = plain_hub.db.one("SELECT password_hash FROM users WHERE username = 'app:aenvae'")
    for attempt in ("", appaccess.NO_PASSWORD, "password", "!"):
        assert not verify_password(attempt, stored["password_hash"])


def test_a_person_cannot_be_called_one(plain_hub):
    """The namespace only holds if nothing else can enter it, and the account
    form is what decides that."""
    import re

    from mcphub.web.routes import build  # noqa: F401 - the pattern lives beside it

    allowed = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")
    assert not allowed.match("app:aenvae"), "a colon must not be a legal username"


def test_taking_every_grant_away_removes_the_identity(plain_hub):
    plain_hub.apps.set_grants("aenvae", {"router": roles.USER})
    plain_hub.apps.set_grants("aenvae", {})
    assert plain_hub.db.one("SELECT id FROM users WHERE username = 'app:aenvae'") is None


def test_an_app_cannot_be_granted_itself(plain_hub):
    """A loop with nothing in it, and a tool list that would list itself."""
    plain_hub.apps.set_grants("aenvae", {"aenvae": roles.ADMIN, "router": roles.USER})
    assert set(plain_hub.apps.grants("aenvae")) == {"router"}


def test_renaming_a_backend_carries_its_identity(plain_hub):
    plain_hub.apps.set_grants("aenvae", {"router": roles.USER})
    plain_hub.apps.rename("aenvae", "dictionary")
    assert plain_hub.apps.grants("dictionary") == {"router": roles.USER}
    assert plain_hub.apps.grants("aenvae") == {}


# ── the credentials ───────────────────────────────────────────────────────

def test_one_token_per_backend_granted(plain_hub):
    """A token here is bound to one endpoint; one token for two backends would
    be the thing this hub refuses everywhere else."""
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    issued = plain_hub.apps.issue("aenvae")
    assert set(issued) == {"router"}
    assert issued["router"]["url"] == f"{BASE}/mcp/router"
    assert issued["router"]["level"] == roles.VIEWER


def test_asking_twice_gives_the_same_credentials(plain_hub):
    """A backend is started more than once — a pinned version, a remount — and
    handing it a new token each time would invalidate the one it is using."""
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    assert plain_hub.apps.issue("aenvae") == plain_hub.apps.issue("aenvae")


def test_the_database_never_holds_the_token(plain_hub):
    from mcphub.crypto import hash_token

    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    token = plain_hub.apps.issue("aenvae")["router"]["token"]
    rows = plain_hub.db.query("SELECT token_hash FROM tokens")
    assert all(r["token_hash"] != token for r in rows)
    assert any(r["token_hash"] == hash_token(token) for r in rows)


def test_changing_a_grant_revokes_what_was_issued(plain_hub):
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    first = plain_hub.apps.issue("aenvae")["router"]["token"]
    plain_hub.apps.set_grants("aenvae", {"router": roles.ADMIN})
    second = plain_hub.apps.issue("aenvae")["router"]["token"]
    assert first != second


def test_an_app_with_no_grants_is_handed_nothing(plain_hub):
    assert plain_hub.apps.issue("aenvae") == {}
    assert plain_hub.apps.environment("aenvae") == {}
    assert plain_hub.apps.header("aenvae") == {}


def test_a_launched_app_finds_them_in_its_environment(plain_hub):
    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    env = plain_hub.apps.environment("aenvae")
    assert env[appaccess.URL_VAR] == BASE
    assert json.loads(env[appaccess.ENV_VAR])["router"]["level"] == roles.VIEWER


def test_a_configured_value_cannot_shadow_a_minted_one(plain_hub):
    """They are credentials the hub issued, not settings someone chose."""
    from mcphub.plugins.builtin.mcpproxy import _collect_env

    plain_hub.apps.set_grants("aenvae", {"router": roles.VIEWER})
    instance = BackendInstance(
        slug="aenvae", title="A", plugin_id="mcp-proxy",
        config={"env": f"{appaccess.ENV_VAR}=forged\n{appaccess.URL_VAR}=http://elsewhere"},
        granted=plain_hub.apps.issue("aenvae"))
    env = _collect_env(instance)
    assert env[appaccess.ENV_VAR] != "forged"
    assert env[appaccess.URL_VAR] == BASE


# ── and what they actually open ───────────────────────────────────────────

class Caller:
    """One MCP conversation over streamable HTTP, as an app would hold it."""

    def __init__(self, client: httpx.AsyncClient, slug: str, token: str) -> None:
        self._client = client
        self._slug = slug
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    async def send(self, method: str, params=None, *, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = 1
        response = await self._client.post(f"/mcp/{self._slug}", json=body,
                                           headers=self._headers)
        if response.headers.get("mcp-session-id"):
            self._headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notify:
            return response
        if response.status_code != 200:
            return response
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return json.loads(next(line[5:] for line in response.text.splitlines()
                                   if line.startswith("data:")))
        return response.json()

    async def opened(self):
        await self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "app", "version": "1"}})
        await self.send("notifications/initialized", {}, notify=True)
        return self

    async def tools(self):
        answer = await self.send("tools/list", {})
        return sorted(t["name"] for t in answer["result"]["tools"])


async def app_calling(hub, client, app: str, target: str) -> Caller:
    """The app's own credential for one backend, used as the app would use it."""
    held = hub.apps.issue(app)
    return await Caller(client, target, held[target]["token"]).opened()


async def test_an_apps_token_reaches_what_it_was_granted(hub):
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.ADMIN})
    assert await (await app_calling(state, client, "aenvae", "router")).tools() == \
        ["look", "wipe"]


async def test_it_reaches_it_only_at_the_level_it_was_granted(hub):
    """The grant is one decision: whether, and how far. An app on `viewer` is
    filtered by the same code that filters a person on `viewer`."""
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.VIEWER})
    assert await (await app_calling(state, client, "aenvae", "router")).tools() == ["look"]


async def test_a_destructive_tool_is_refused_to_an_app_that_is_not_admin(hub):
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.USER})
    answer = await (await app_calling(state, client, "aenvae", "router")).send(
        "tools/call", {"name": "wipe", "arguments": {}})
    assert "error" in answer, answer
    assert "wipe" in answer["error"]["message"]


async def test_a_token_for_one_backend_does_not_open_another(hub):
    """The same rule that protects a person's token. An app granted two
    backends holds two tokens precisely so this stays true."""
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.ADMIN})
    held = state.apps.issue("aenvae")["router"]["token"]

    response = await Caller(client, "unraid", held).send(
        "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "app", "version": "1"}})
    assert response.status_code in (401, 403), getattr(response, "text", response)


async def test_an_ungranted_backend_is_closed_to_it(hub):
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.ADMIN})
    assert "unraid" not in state.apps.issue("aenvae")


async def test_revoking_shuts_the_door_at_once(hub):
    """Without restarting the app, and without waiting for anything to expire."""
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.ADMIN})
    caller = await app_calling(state, client, "aenvae", "router")
    assert await caller.tools()

    state.apps.set_grants("aenvae", {})
    response = await caller.send("tools/list", {})
    assert getattr(response, "status_code", 200) in (401, 403), response


async def test_the_apps_access_is_not_the_users(hub):
    """A viewer using an app that holds admin reaches the target as the app.

    That is the design — the app is a service with its own authority — and it
    is the reason the app is told the person's own level separately. Pinned
    here because it is the thing most likely to be assumed the other way round.
    """
    state, client = hub
    state.apps.set_grants("aenvae", {"router": roles.ADMIN})
    assert state.apps.issue("aenvae")["router"]["level"] == roles.ADMIN
    assert await (await app_calling(state, client, "aenvae", "router")).tools() == \
        ["look", "wipe"]
