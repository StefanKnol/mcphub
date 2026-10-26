"""Running one tool by hand to see what it actually returns.

When a tool misbehaves there are two questions — what did the backend say, and
is the hub changing it on the way — and from outside they look identical. One
call answers both: the backend's own result, and what an MCP client is handed
after the hub has finished with it.

One call, not two. Showing two answers by making two requests would run the
tool twice, which for anything that writes is not a diagnostic.
"""

import json
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp_types import ToolAnnotations

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance, CheckResult, PluginDefaults, ToolProbe
from mcphub.web.routes import argument_skeleton, tools_to_try

BASE = "http://127.0.0.1:8080"
PASSWORD = "correct horse battery"


class Answering(PluginDefaults):
    """A backend with one quiet tool and one that destroys something."""

    id, name, description, fields = "answering", "Answering", "d", ()

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError

        server = MCPServer(instance.title)

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def look(word: str, loudly: bool = False) -> str:
            """Look a word up."""
            if word == "boom":
                raise ToolError("there is no such word")
            return f"{word} means {word}" + ("!" if loudly else "")

        @server.tool(annotations=ToolAnnotations(destructiveHint=True))
        def wipe() -> str:
            """Remove everything."""
            return "gone"

        return server

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")


@pytest.fixture
async def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=BASE,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Answering())
    state.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
        "updated_at) VALUES ('dictionary', 'answering', 'Dictionary', 0, '{}', ?, ?)",
        (utcnow(), utcnow()))
    for name, manages in (("boss", 1), ("reader", 0)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, "
            "created_at) VALUES (?, ?, 0, ?, ?)",
            (name, hash_password(PASSWORD), manages, utcnow()))
    async with app.router.lifespan_context(app):
        yield state, app


async def signed_in(app, username: str = "boss") -> httpx.AsyncClient:
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                follow_redirects=False)
    await browser.post("/login", data={"username": username, "password": PASSWORD})
    return browser


async def run(browser, tool: str, arguments="{}", slug: str = "dictionary"):
    return (await browser.post(f"/backends/{slug}/try",
                               data={"tool": tool, "arguments": arguments})).json()


# ── what comes back ───────────────────────────────────────────────────────

async def test_it_shows_what_a_client_would_receive(hub):
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"word": "aenvae"}))
    assert answer["ok"]
    assert answer["delivered"] == "aenvae means aenvae"


async def test_it_shows_the_backends_own_answer_too(hub):
    """The half that says whether the hub is the problem."""
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"word": "aenvae"}))
    assert answer["raw"]["content"][0]["text"] == "aenvae means aenvae"


async def test_it_says_how_long_it_took(hub):
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"word": "x"}))
    assert isinstance(answer["took_ms"], int)


async def test_optional_arguments_are_sent_when_given(hub):
    _, app = hub
    answer = await run(await signed_in(app), "look",
                       json.dumps({"word": "x", "loudly": True}))
    assert answer["delivered"] == "x means x!"


async def test_a_tool_that_refuses_is_reported_rather_than_hidden(hub):
    """The reason it refused is the thing being looked for."""
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"word": "boom"}))
    assert answer["ok"] is False
    assert "no such word" in answer["detail"]


async def test_arguments_that_do_not_fit_the_schema_say_so(hub):
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"nonsense": 1}))
    assert answer["ok"] is False
    assert answer["detail"]


async def test_an_unknown_tool_says_so(hub):
    _, app = hub
    answer = await run(await signed_in(app), "nope")
    assert answer["ok"] is False


# ── and what it refuses to do ─────────────────────────────────────────────

@pytest.mark.parametrize("written", ["{", "[1, 2]", '"a string"', "null"])
async def test_arguments_must_be_a_json_object(hub, written):
    _, app = hub
    answer = await run(await signed_in(app), "look", written)
    assert answer["ok"] is False
    assert "JSON" in answer["detail"]


async def test_no_tool_named_runs_nothing(hub):
    _, app = hub
    answer = await run(await signed_in(app), "")
    assert answer["ok"] is False
    assert "Pick a tool" in answer["detail"]


async def test_an_account_that_cannot_configure_cannot_run(hub):
    """A smaller thing than configuring the backend that offers it, but not a
    thing to hand out more widely than the page it lives on."""
    _, app = hub
    answer = await (await signed_in(app, "reader")).post(
        "/backends/dictionary/try", data={"tool": "look", "arguments": "{}"})
    assert answer.status_code == 403


async def test_signing_in_is_required(hub):
    _, app = hub
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    assert (await browser.post("/backends/dictionary/try",
                               data={"tool": "look"})).status_code == 401


async def test_a_backend_that_is_not_there_says_so(hub):
    _, app = hub
    answer = await (await signed_in(app)).post("/backends/nope/try", data={"tool": "look"})
    assert answer.status_code == 404


# ── the form it offers ────────────────────────────────────────────────────

async def test_it_offers_the_tools_a_client_would_see(hub):
    state, _ = hub
    offered = await tools_to_try(state.registry.get("answering"),
                                 state.current_instance("dictionary"))
    assert [t["name"] for t in offered] == ["look", "wipe"]


async def test_it_marks_the_destructive_one(hub):
    """So the page can ask before running it, rather than after."""
    state, _ = hub
    offered = {t["name"]: t for t in await tools_to_try(
        state.registry.get("answering"), state.current_instance("dictionary"))}
    assert offered["wipe"]["destructive"] is True
    assert offered["look"]["destructive"] is False


async def test_a_backend_with_no_tools_offers_nothing_to_run(hub):
    state, _ = hub
    assert await tools_to_try(state.registry.get("answering"), None) == []


async def test_the_page_carries_the_panel(hub):
    _, app = hub
    page = (await (await signed_in(app)).get("/backends/dictionary")).text
    assert "Try a tool" in page
    assert 'value="look"' in page
    assert "It really runs" in page, "running a tool is not a rehearsal"


# ── the argument skeleton ─────────────────────────────────────────────────

def test_required_parameters_come_first():
    """So the ones that must be filled are the ones in front of you."""
    written = argument_skeleton({
        "type": "object",
        "properties": {"optional": {"type": "string"}, "needed": {"type": "string"}},
        "required": ["needed"],
    })
    assert list(json.loads(written)) == ["needed", "optional"]


def test_optional_parameters_are_there_too():
    """Usually the optional one is what you are trying the tool to find out
    about, and deleting a line beats reading a schema to learn it exists."""
    assert "optional" in argument_skeleton({
        "type": "object", "properties": {"optional": {"type": "string"}}})


@pytest.mark.parametrize("kind,placeholder", [
    ("string", ""), ("number", 0), ("integer", 0), ("boolean", False),
    ("array", []), ("object", {}), ("nothing-like-that", None),
])
def test_each_type_gets_a_slot_of_its_own_shape(kind, placeholder):
    written = json.loads(argument_skeleton({
        "type": "object", "properties": {"k": {"type": kind}}}))
    assert written["k"] == placeholder


def test_a_default_is_used_when_there_is_one():
    written = json.loads(argument_skeleton({
        "type": "object", "properties": {"k": {"type": "string", "default": "Europe/Amsterdam"}}}))
    assert written["k"] == "Europe/Amsterdam"


def test_an_enum_offers_one_of_its_values():
    written = json.loads(argument_skeleton({
        "type": "object", "properties": {"k": {"enum": ["a", "b"]}}}))
    assert written["k"] == "a"


def test_a_nullable_type_is_read_as_the_type():
    written = json.loads(argument_skeleton({
        "type": "object", "properties": {"k": {"type": ["string", "null"]}}}))
    assert written["k"] == ""


@pytest.mark.parametrize("schema", [None, {}, {"type": "object"}, "not a schema"])
def test_a_tool_that_takes_nothing_gets_an_empty_object(schema):
    assert argument_skeleton(schema) == "{}"


# ── what the proxy drops on the way ───────────────────────────────────────

def upstream_said(**fields):
    from mcp_types import CallToolResult, TextContent

    return CallToolResult(**{"content": [TextContent(type="text", text="hello")], **fields})


def test_structured_content_beside_text_is_pointed_at():
    """A real difference between what the server answered and what the model
    sees, and one that is easy to miss in two panes of JSON."""
    from mcphub.plugins.builtin.mcpproxy import _losses

    notes = _losses(upstream_said(structuredContent={"a": 1}))
    assert any("structured" in n for n in notes)


def test_content_that_is_not_text_is_pointed_at():
    from mcp_types import ImageContent

    from mcphub.plugins.builtin.mcpproxy import _losses

    notes = _losses(upstream_said(
        content=[ImageContent(type="image", data="aGk=", mimeType="image/png")]))
    assert any("image" in n for n in notes)


def test_a_plain_text_answer_has_nothing_to_point_at():
    from mcphub.plugins.builtin.mcpproxy import _losses

    assert _losses(upstream_said()) == ()


def test_structured_content_on_its_own_is_delivered_rather_than_lost():
    """With no text blocks it becomes the text, so nothing is dropped."""
    from mcphub.plugins.builtin.mcpproxy import _losses

    assert _losses(upstream_said(content=[], structuredContent={"a": 1})) == ()


async def test_the_notes_reach_the_page(hub):
    """They are the answer to the question that brought someone here."""
    _, app = hub
    answer = await run(await signed_in(app), "look", json.dumps({"word": "x"}))
    assert "notes" in answer


# ── the default, for a plugin that is not a proxy ─────────────────────────

async def test_a_plugin_that_says_nothing_still_gets_a_probe(hub):
    """Through the server it builds, so what is reported is what a client
    would receive — which is the question being asked."""
    state, _ = hub
    probe = await state.registry.get("answering").try_tool(
        state.current_instance("dictionary"), "look", {"word": "aenvae"})
    assert isinstance(probe, ToolProbe)
    assert probe.ok and probe.delivered == "aenvae means aenvae"


async def test_the_default_reports_a_refusal_rather_than_raising(hub):
    state, _ = hub
    probe = await state.registry.get("answering").try_tool(
        state.current_instance("dictionary"), "look", {"word": "boom"})
    assert probe.ok is False and "no such word" in probe.detail
