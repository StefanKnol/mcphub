"""How far a grant goes, enforced end to end.

A grant says an account may reach a backend. A level says how much of it they
get, and the hub decides that itself from the annotations a server already
publishes — `readOnlyHint` and `destructiveHint` — rather than asking the
backend to police its own callers. That is the whole point: it works on a
server that has never heard of this hub.

The awkward part is that every level is served by *one* process. Filtering
per caller inside a shared server is what avoids a process per
(backend, version, level), so these tests drive real HTTP requests through the
real mount with real tokens, and check that two accounts talking to the same
running server are told different things.
"""

import json
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcphub import roles
from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password, hash_token
from mcphub.db import utcnow
from mcphub.plugins.base import CheckResult, PluginDefaults

BASE = "http://127.0.0.1:8080"
SLUG = "router"


class Annotated(PluginDefaults):
    """A backend that annotates its tools the way the specification asks."""

    id, name, description, fields = "annotated", "Annotated", "d", ()

    def build(self, instance):
        server = MCPServer(instance.title)

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def list_rules() -> str:
            return "listed"

        @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        def add_rule() -> str:
            return "added"

        @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
        def remove_rule() -> str:
            return "removed"

        @server.tool()
        def unannotated() -> str:
            return "ran"

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def whoami() -> str:
            """Who the hub says is calling, as a plugin would ask."""
            token = get_access_token()
            return token.subject if token else "nobody"

        return server

    async def check(self, instance):  # pragma: no cover - never called here
        return CheckResult(True, "ok")


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Annotated())
    state.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, updated_at) "
        "VALUES (?, 'annotated', 'Router', 1, '{}', ?, ?)", (SLUG, utcnow(), utcnow()))
    backend_id = state.db.one("SELECT id FROM backends WHERE slug = ?", (SLUG,))["id"]

    for name, level in (("looker", roles.VIEWER), ("operator", roles.USER),
                        ("owner", roles.ADMIN)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, 0, 0, ?)", (name, hash_password("x" * 12), utcnow()))
        user_id = state.db.one("SELECT id FROM users WHERE username = ?", (name,))["id"]
        state.db.execute(
            "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
            "VALUES (?, ?, ?, ?)", (user_id, backend_id, level, utcnow()))
        state.db.execute(
            "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, "
            "expires_at, created_at) VALUES (?, 'access', 'c', ?, 'mcp:use', ?, 9e9, ?)",
            (hash_token(f"tok-{name}"), user_id, f"{BASE}/mcp/{SLUG}", utcnow()))

    async with app.router.lifespan_context(app):
        yield state, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


class Caller:
    """One MCP conversation over streamable HTTP, as a real client holds it."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    async def send(self, method: str, params: dict | None = None, *, notify: bool = False):
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = 1
        response = await self._client.post(f"/mcp/{SLUG}", json=body, headers=self._headers)
        session = response.headers.get("mcp-session-id")
        if session:
            self._headers["Mcp-Session-Id"] = session
        if notify:
            return response
        assert response.status_code == 200, response.text
        return _payload(response)

    async def opened(self):
        await self.send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        await self.send("notifications/initialized", {}, notify=True)
        return self

    async def tools(self) -> list[str]:
        result = await self.send("tools/list", {})
        assert "error" not in result, result
        return sorted(t["name"] for t in result["result"]["tools"])

    async def call(self, name: str) -> dict:
        return await self.send("tools/call", {"name": name, "arguments": {}})


def _payload(response: httpx.Response) -> dict:
    """One JSON-RPC message, whether it arrived as JSON or as a one-event stream."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise AssertionError(f"no data event in {response.text!r}")
    return response.json()


async def caller(client, name) -> Caller:
    return await Caller(client, f"tok-{name}").opened()


# ── what each level is offered ────────────────────────────────────────────

@pytest.mark.parametrize("account,expected", [
    ("looker", ["list_rules", "whoami"]),
    ("operator", ["add_rule", "list_rules", "unannotated", "whoami"]),
    ("owner", ["add_rule", "list_rules", "remove_rule", "unannotated", "whoami"]),
])
async def test_the_tool_list_is_cut_to_the_level(hub, account, expected):
    _, client = hub
    assert await (await caller(client, account)).tools() == expected


async def test_an_unannotated_tool_is_withheld_from_a_viewer(hub):
    """Nothing says it only reads, so a read-only level cannot include it.

    The alternative — offering it because nothing said not to — would make
    `viewer` meaningless on the many servers that annotate nothing at all.
    """
    _, client = hub
    assert "unannotated" not in await (await caller(client, "looker")).tools()
    assert "unannotated" in await (await caller(client, "operator")).tools()


async def test_one_running_server_answers_every_level_differently(hub):
    """The filtering has to happen per caller, not per process.

    A process per (backend, version, level) would triple the subprocesses a
    hub runs for no benefit, so this is the property that makes the design
    affordable — and it is only true if nothing about the level leaks into
    the server that is built.
    """
    state, client = hub
    assert len(await (await caller(client, "looker")).tools()) == 2
    assert len(await (await caller(client, "owner")).tools()) == 5
    assert len(state.mounts._mounted[SLUG].variants) == 1


# ── and what it may actually run ──────────────────────────────────────────

async def test_a_withheld_tool_cannot_be_called_anyway(hub):
    """A client can call a tool it was never offered; hiding is not refusing."""
    _, client = hub
    answer = await (await caller(client, "looker")).call("add_rule")
    assert "error" in answer, answer
    assert "viewer" in answer["error"]["message"]
    assert "add_rule" in answer["error"]["message"]


async def test_a_destructive_tool_is_refused_to_an_ordinary_account(hub):
    _, client = hub
    answer = await (await caller(client, "operator")).call("remove_rule")
    assert "error" in answer, answer
    assert "remove_rule" in answer["error"]["message"]


@pytest.mark.parametrize("account,tool", [
    ("looker", "list_rules"), ("operator", "add_rule"), ("owner", "remove_rule"),
])
async def test_a_permitted_tool_still_runs(hub, account, tool):
    _, client = hub
    answer = await (await caller(client, account)).call(tool)
    assert "error" not in answer, answer


async def test_a_tool_list_shaped_per_account_is_never_shared_cached(hub):
    """Two accounts get different answers, so nothing may hold one for both."""
    _, client = hub
    result = await (await caller(client, "owner")).send("tools/list", {})
    assert result["result"].get("cacheScope", "private") == "private"


# ── and that it keeps tracking the account ────────────────────────────────

async def test_a_level_change_lands_on_the_next_request_of_an_open_session(hub):
    """The same property the grant check already has, for the same reason.

    An MCP session outlives the request that opened it, so a level read once at
    connect time would leave an account holding what it had until it happened
    to reconnect — which for a long-lived connector is indefinitely.
    """
    state, client = hub
    session = await caller(client, "operator")
    assert "remove_rule" not in await session.tools()

    state.db.execute("UPDATE backend_grants SET role = 'admin' WHERE user_id = "
                     "(SELECT id FROM users WHERE username = 'operator')")
    assert "remove_rule" in await session.tools(), (
        "the level must be read per request, not held for the life of a session"
    )
    assert "error" not in await session.call("remove_rule")


async def test_revoking_the_grant_outright_closes_the_door_too(hub):
    state, client = hub
    session = await caller(client, "owner")
    assert await session.tools()

    state.db.execute("DELETE FROM backend_grants WHERE user_id = "
                     "(SELECT id FROM users WHERE username = 'owner')")
    response = await client.post(f"/mcp/{SLUG}",
                                 json={"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                       "params": {}},
                                 headers=session._headers)
    assert response.status_code == 403


async def test_a_backend_can_find_out_who_is_calling(hub):
    """`get_access_token()` inside a tool, which is how a plugin would ask.

    The SDK installs the middleware that makes this work only when a server
    owns its own authentication. These do not — the hub authenticates outside
    the mount — so without the hub adding it, every plugin would look up the
    caller and find nobody.
    """
    _, client = hub
    answer = await (await caller(client, "looker")).call("whoami")
    assert "error" not in answer, answer
    assert "looker" in json.dumps(answer["result"])


# ── what a trusted app is told ────────────────────────────────────────────

@pytest.fixture
def upstream(monkeypatch):
    """Capture what the hub sends an app's own web interface."""
    from mcphub.web import uiproxy

    sent: list[httpx.Headers] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        return httpx.Response(200, content=b"<html></html>",
                              headers={"content-type": "text/html"})

    def client(*_args, **_kwargs):
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(uiproxy.httpx2, "AsyncClient", client)
    return sent


async def app_backend(state, *, trusted: bool) -> None:
    state.db.execute(
        "UPDATE backends SET config_json = ? WHERE slug = ?",
        (json.dumps({"ui_url": "http://app.test", "ui_proxy": True, "ui_trusted": trusted}), SLUG))


async def signed_in(client, name: str) -> httpx.AsyncClient:
    await client.post("/login", data={"username": name, "password": "x" * 12})
    return client


@pytest.mark.parametrize("account,expected", [
    ("looker", roles.VIEWER), ("operator", roles.USER), ("owner", roles.ADMIN),
])
async def test_a_trusted_app_is_told_the_level_as_well_as_the_account(
        hub, upstream, account, expected):
    """Over MCP the hub enforces the level itself, because tools say what they
    do. Over HTTP a POST is just a POST, so an app that wants to honour the
    same levels has to be told which one applies and decide for itself."""
    state, client = hub
    await app_backend(state, trusted=True)
    await signed_in(client, account)

    response = await client.get(f"/ui/{SLUG}/")
    assert response.status_code == 200, response.text
    assert upstream[-1]["x-mcphub-user"] == account
    assert upstream[-1]["x-mcphub-role"] == expected


async def test_a_sandboxed_app_is_told_nothing_about_the_account(hub, upstream):
    state, client = hub
    await app_backend(state, trusted=False)
    await signed_in(client, "owner")

    assert (await client.get(f"/ui/{SLUG}/")).status_code == 200
    assert "x-mcphub-role" not in upstream[-1]
    assert "x-mcphub-user" not in upstream[-1]


# ── reading what a tool says it does ──────────────────────────────────────

def as_wire(tool: dict) -> dict:
    """The same tool as a serialised `tools/list` entry carries it."""
    return {"name": tool["name"], "annotations": {
        "readOnlyHint": tool.get("ro"), "destructiveHint": tool.get("destr")}}


def as_model(tool: dict):
    from mcp_types import Tool

    return Tool(name=tool["name"], inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=tool.get("ro"),
                                            destructiveHint=tool.get("destr")))


@pytest.mark.parametrize("shape", [as_wire, as_model])
@pytest.mark.parametrize("tool,allowed", [
    ({"name": "read", "ro": True}, {roles.VIEWER, roles.USER, roles.ADMIN}),
    ({"name": "write", "ro": False}, {roles.USER, roles.ADMIN}),
    ({"name": "wipe", "ro": False, "destr": True}, {roles.ADMIN}),
    ({"name": "quiet"}, {roles.USER, roles.ADMIN}),
])
def test_both_shapes_of_a_tool_read_the_same(shape, tool, allowed):
    """A result that has been serialised carries `readOnlyHint`, one that has
    not carries `read_only_hint`. Reading only the second found no annotations
    on the path that actually runs, and offered a viewer nothing at all."""
    for level in roles.LEVELS:
        assert roles.allows(level, shape(tool)) is (level in allowed)


def test_an_annotation_that_is_not_a_boolean_is_not_trusted():
    """A string `"true"` must not read as a promise either way."""
    odd = {"name": "x", "annotations": {"readOnlyHint": "true", "destructiveHint": "false"}}
    assert roles.allows(roles.VIEWER, odd) is False
    assert roles.allows(roles.USER, odd) is True
