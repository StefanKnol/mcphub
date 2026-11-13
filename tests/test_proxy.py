"""Proxy plugin: schema fidelity and the tool allowlist."""

import json

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool as UpstreamTool

from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.mcpproxy import PLUGIN, _allowed
from mcphub.plugins.builtin.mcpproxy.mirror import _build_signature, _flatten, mirror_tool

UPSTREAM_TOOLS = [
    {
        "name": "list_containers",
        "description": "List all Docker containers.",
        "inputSchema": {
            "type": "object",
            "properties": {"state": {"type": "string", "description": "running, stopped, or all"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "system_reboot",
        "description": "Reboot the server.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": True, "title": "Reboot"},
    },
    {
        "name": "set_fan_speed",
        "description": "Set a fan's speed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fan": {"type": "string", "description": "Fan id"},
                "percent": {"type": "integer", "description": "0-100"},
                "mode": {"type": "string", "enum": ["auto", "manual"]},
            },
            "required": ["fan", "percent"],
        },
    },
]


class FakeUpstream:
    def __init__(self, result=None, fail=None):
        self.calls = []
        self._result = result
        self._fail = fail

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._fail:
            raise self._fail
        return self._result


class Result:
    def __init__(self, text=None, is_error=False, structured=None):
        self.content = [type("B", (), {"type": "text", "text": text})()] if text is not None else []
        self.is_error = is_error
        self.structured_content = structured


def instance(**config):
    return BackendInstance(slug="up", title="Up", plugin_id="mcp-proxy", config=config)


# ── schema synthesis ──────────────────────────────────────────────────────

def test_required_and_optional_parameters_survive():
    sig, _, skipped = _build_signature(UPSTREAM_TOOLS[2]["inputSchema"])
    assert skipped == []
    assert list(sig.parameters) == ["fan", "percent", "mode"]
    assert sig.parameters["fan"].default is sig.empty
    assert sig.parameters["mode"].default is None


def test_required_parameters_are_ordered_first():
    """Python forbids a parameter without a default after one that has one."""
    sig, _, _ = _build_signature({
        "type": "object",
        "properties": {"optional": {"type": "string"}, "needed": {"type": "string"}},
        "required": ["needed"],
    })
    assert list(sig.parameters) == ["needed", "optional"]


def test_no_argument_schema_yields_no_parameters():
    sig, _, _ = _build_signature({"type": "object", "properties": {}})
    assert list(sig.parameters) == []


def test_a_name_python_cannot_use_gets_a_stand_in_with_the_real_name_as_its_alias():
    sig, _, skipped = _build_signature({
        "type": "object",
        "properties": {"fine": {"type": "string"}, "not-an-identifier": {"type": "string"}, "class": {"type": "string"}},
    })
    assert skipped == []
    assert list(sig.parameters) == ["fine", "argument_0", "argument_1"]
    aliases = [sig.parameters[name].annotation.__metadata__[0].alias for name in sig.parameters]
    assert aliases == [None, "not-an-identifier", "class"]


def test_a_stand_in_never_takes_a_name_the_upstream_uses():
    sig, _, _ = _build_signature({
        "type": "object",
        "properties": {"argument_0": {"type": "string"}, "class": {"type": "string"}},
    })
    assert list(sig.parameters) == ["argument_0", "argument_1"]
    assert sig.parameters["argument_1"].annotation.__metadata__[0].alias == "class"


def test_only_a_property_that_is_not_a_schema_is_reported_as_skipped():
    sig, _, skipped = _build_signature({"type": "object", "properties": {"fine": {"type": "string"}, "broken": "not a schema"}})
    assert list(sig.parameters) == ["fine"]
    assert skipped == ["broken"]


async def test_mirrored_tool_reproduces_the_upstream_schema():
    mcp = MCPServer("t")
    up = FakeUpstream(Result("ok"))
    for raw in UPSTREAM_TOOLS:
        assert mirror_tool(mcp, up, UpstreamTool.model_validate(raw))

    by_name = {t.name: t for t in await mcp.list_tools()}
    assert set(by_name) == {"list_containers", "system_reboot", "set_fan_speed"}

    fan = by_name["set_fan_speed"].input_schema
    assert set(fan["properties"]) == {"fan", "percent", "mode"}
    assert set(fan.get("required", [])) == {"fan", "percent"}
    assert fan["properties"]["percent"]["description"] == "0-100"
    assert by_name["list_containers"].description == "List all Docker containers."


async def test_destructive_hint_is_carried_through():
    """Losing it would make a reboot tool look harmless to the client's prompt."""
    mcp = MCPServer("t")
    mirror_tool(mcp, FakeUpstream(Result("ok")), UpstreamTool.model_validate(UPSTREAM_TOOLS[1]))
    tool = (await mcp.list_tools())[0]
    assert tool.annotations is not None
    assert tool.annotations.destructive_hint is True


# ── forwarding ────────────────────────────────────────────────────────────

async def test_call_forwards_only_supplied_arguments():
    mcp = MCPServer("t")
    up = FakeUpstream(Result("done"))
    mirror_tool(mcp, up, UpstreamTool.model_validate(UPSTREAM_TOOLS[2]))

    result = await mcp.call_tool("set_fan_speed", {"fan": "fan1", "percent": 50})
    assert result.content[0].text == "done"
    # `mode` was not supplied, so it is omitted rather than sent as null.
    assert up.calls == [("set_fan_speed", {"fan": "fan1", "percent": 50})]


async def test_upstream_error_result_becomes_a_tool_error():
    mcp = MCPServer("t")
    mirror_tool(mcp, FakeUpstream(Result("upstream said no", is_error=True)),
                UpstreamTool.model_validate(UPSTREAM_TOOLS[1]))
    with pytest.raises(ToolError) as excinfo:
        await mcp.call_tool("system_reboot", {})
    assert "upstream said no" in str(excinfo.value)


def test_flatten_prefers_text_then_structured():
    assert _flatten(Result("hello")) == "hello"
    assert json.loads(_flatten(Result(None, structured={"a": 1}))) == {"a": 1}
    assert _flatten(Result(None)) == ""


def test_flatten_reports_content_it_cannot_render():
    result = Result(None)
    result.content = [type("B", (), {"type": "image"})()]
    assert "image" in _flatten(result), "dropping content silently would look like an empty answer"


# ── the allowlist ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored,expected", [
    ([], None),
    ("", None),
    (None, None),
    (["a", "b"], {"a", "b"}),
    ("a, b", {"a", "b"}),
])
def test_allowlist_parsing(stored, expected):
    assert _allowed(instance(tools=stored)) == expected


def test_empty_allowlist_exposes_everything():
    """The default has to be permissive, or a freshly added backend does nothing."""
    assert _allowed(instance()) is None


def test_build_exposes_only_selected_tools():
    catalog = json.dumps(UPSTREAM_TOOLS)
    server = PLUGIN.build(instance(url="http://x/mcp", tool_catalog=catalog, tools=["list_containers"]))
    import asyncio
    names = [t.name for t in asyncio.run(server.list_tools())]
    assert names == ["list_containers"]
    assert "system_reboot" not in names, "a filtered tool must not be reachable, not merely hidden"


def test_build_without_a_catalog_still_mounts():
    """An unreachable upstream must not stop the endpoint from coming up."""
    server = PLUGIN.build(instance(url="http://unreachable/mcp"))
    import asyncio
    assert asyncio.run(server.list_tools()) == []


# ── the schema a client is offered is the upstream's own ───────────────────

NESTED_TOOL = {
    "name": "save_entry",
    "description": "Save an entry and what is known about it.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Its title."},
            "facts": {
                "type": "array",
                "description": "What is known.",
                "default": [],
                "items": {
                    "type": "object",
                    "properties": {
                        "property": {"type": "string", "description": "What is being said."},
                        "value": {"type": "string"},
                        "certain": {"type": "boolean", "default": False},
                    },
                    "required": ["property", "value"],
                    "additionalProperties": False,
                },
            },
            "place": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "floor": {"type": "integer"}},
                "required": ["name"],
            },
            "count": {"anyOf": [{"type": "number"}, {"type": "string"}], "description": "Exact."},
            "kind": {"type": "string", "enum": ["person", "place"], "default": "person"},
        },
        "required": ["title"],
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    },
}


async def test_a_nested_schema_reaches_the_client_exactly_as_the_upstream_wrote_it():
    """An array of objects must not arrive as an array of anything.

    The signature this proxy synthesises can say "a list" and no more, so a
    schema rebuilt from it loses every field of the items. A client then has
    to guess them.
    """
    mcp = MCPServer("t")
    assert mirror_tool(mcp, FakeUpstream(Result("ok")), UpstreamTool.model_validate(NESTED_TOOL))
    offered = (await mcp.list_tools())[0].input_schema
    assert offered == NESTED_TOOL["inputSchema"]
    # Nothing the SDK generates from a signature is in it.
    assert "title" not in offered["properties"]["facts"]
    assert offered["properties"]["facts"]["items"]["properties"]["property"]["description"] == "What is being said."
    assert "anyOf" not in offered["properties"]["kind"], "an optional parameter is not offered null beside its type"


async def test_the_offered_schema_is_a_copy_the_proxy_cannot_change_under_the_upstream():
    mcp = MCPServer("t")
    upstream_tool = UpstreamTool.model_validate(NESTED_TOOL)
    mirror_tool(mcp, FakeUpstream(Result("ok")), upstream_tool)
    offered = (await mcp.list_tools())[0].input_schema
    offered["properties"]["facts"]["items"]["properties"].clear()
    assert upstream_tool.input_schema["properties"]["facts"]["items"]["properties"], "the upstream's schema was edited"


async def test_nested_arguments_are_forwarded_untouched():
    mcp = MCPServer("t")
    up = FakeUpstream(Result("saved"))
    mirror_tool(mcp, up, UpstreamTool.model_validate(NESTED_TOOL))
    arguments = {
        "title": "The ford",
        "facts": [{"property": "depth", "value": "to the knee", "certain": True}, {"property": "where", "value": "below the mill"}],
        "place": {"name": "The mill", "floor": 2},
        "count": "12.5",
    }
    await mcp.call_tool("save_entry", arguments)
    assert up.calls == [("save_entry", arguments)]


async def test_a_parameter_of_several_types_takes_each_of_them():
    """The signature used to keep the first type only and refuse the rest."""
    mcp = MCPServer("t")
    up = FakeUpstream(Result("ok"))
    mirror_tool(mcp, up, UpstreamTool.model_validate({
        "name": "set_level",
        "inputSchema": {"type": "object", "properties": {"level": {"type": ["string", "integer"]}}, "required": ["level"]},
    }))
    await mcp.call_tool("set_level", {"level": 3})
    await mcp.call_tool("set_level", {"level": "high"})
    assert [call[1] for call in up.calls] == [{"level": 3}, {"level": "high"}]


async def test_a_parameter_python_cannot_name_is_offered_and_forwarded_under_its_own_name():
    """`from`, `class` and `not-an-identifier` are ordinary JSON names.

    They used to be dropped, with a note in the description saying so, which
    left a tool whose required `from` could never be given.
    """
    schema = {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "Who it is now."},
            "to": {"type": "string"},
            "not-an-identifier": {"type": "integer"},
            "ctx": {"type": "string"},
            "argument_0": {"type": "string", "description": "A name the proxy might have picked for itself."},
        },
        "required": ["from", "to"],
    }
    mcp = MCPServer("t")
    up = FakeUpstream(Result("renamed"))
    assert mirror_tool(mcp, up, UpstreamTool.model_validate({"name": "rename", "description": "Rename.", "inputSchema": schema}))
    tool = (await mcp.list_tools())[0]
    assert tool.input_schema == schema
    assert tool.description == "Rename.", "nothing is said about parameters that could not be represented"

    arguments = {"from": "old", "to": "new", "not-an-identifier": 4, "ctx": "why", "argument_0": "kept"}
    await mcp.call_tool("rename", arguments)
    assert up.calls == [("rename", arguments)]

    with pytest.raises(ToolError):
        await mcp.call_tool("rename", {"to": "new"})  # the required `from` is still required


async def test_a_schema_that_is_not_an_object_schema_still_mounts():
    mcp = MCPServer("t")
    up = FakeUpstream(Result("ok"))
    for name, schema in (("no_schema", {}), ("only_type", {"type": "object"})):
        assert mirror_tool(mcp, up, UpstreamTool.model_validate({"name": name, "inputSchema": schema}))
    by_name = {t.name: t for t in await mcp.list_tools()}
    for name in ("no_schema", "only_type"):
        assert by_name[name].input_schema.get("type") == "object"
        await mcp.call_tool(name, {})


# ── resources and prompts are listed live, with the catalogue as fallback ──
#
# A backend whose resource list is dynamic (a document per story run) used to
# show only what it listed when it was saved. The list is asked of the upstream
# now; the catalogue answers only when the upstream cannot be asked.

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from mcp import Client  # noqa: E402
from mcp.server.mcpserver.resources import FunctionResource  # noqa: E402
from mcp.types import Completion, GetPromptResult, PromptMessage, TextContent  # noqa: E402
from mcp.types import ResourceTemplate as UpstreamResourceTemplate  # noqa: E402
from mcp.types import Prompt as UpstreamPrompt  # noqa: E402
from mcp.types import Resource as UpstreamResource  # noqa: E402

from mcp.server.mcpserver.exceptions import ResourceError  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

import mcphub.plugins.builtin.mcpproxy as proxy_module  # noqa: E402
from mcphub.plugins.builtin.mcpproxy.live import ProxiedServer  # noqa: E402
from mcphub.plugins.builtin.mcpproxy.upstream import (  # noqa: E402
    Upstream,
    UpstreamConfig,
    UpstreamError,
    UpstreamRefusal,
)

SAVED_RESOURCE = {"uri": "story://runs/first", "name": "first run", "mimeType": "text/markdown"}
NEW_RESOURCE = {"uri": "story://runs/second", "name": "second run", "mimeType": "text/markdown"}
UI_RESOURCE = {
    "uri": "ui://clock/app.html",
    "name": "clock app",
    "mimeType": "text/html;profile=mcp-app",
    "_meta": {"ui": {"csp": {"connectDomains": ["https://example.com"]}}},
}
SAVED_PROMPT = {"name": "recap", "description": "Recap a run."}
NEW_PROMPT = {"name": "next_scene", "description": "Plan the next scene.",
              "arguments": [{"name": "run", "required": True}]}


class LiveUpstream:
    """Stands in for `Upstream`, holding what the upstream lists right now."""

    def __init__(self, resources, prompts, *, down=False, delay_seconds=0.0, templates=(),
                 readable=(), blobs=None):
        self._cfg = SimpleNamespace(url="http://upstream/mcp", command="", timeout=30.0)
        self.resources = [UpstreamResource.model_validate(r) for r in resources]
        self.prompts = [UpstreamPrompt.model_validate(p) for p in prompts]
        self.templates = [UpstreamResourceTemplate.model_validate(t) for t in templates]
        # Uris it reads without listing them, as a server with a template does.
        self.readable = set(readable)
        self.blobs = dict(blobs or {})
        self.template_list_calls = 0
        self.completions = []
        self.down = down
        self.delay_seconds = delay_seconds
        self.resource_list_calls = 0
        self.prompt_list_calls = 0
        self.tool_list_calls = 0
        self.reads = []
        self.prompt_calls = []

    async def list_tools(self):
        self.tool_list_calls += 1
        return []

    async def list_resources_once(self):
        self.resource_list_calls += 1
        await asyncio.sleep(self.delay_seconds)
        if self.down:
            raise UpstreamError("upstream is down")
        return list(self.resources)

    async def list_prompts_once(self):
        self.prompt_list_calls += 1
        await asyncio.sleep(self.delay_seconds)
        if self.down:
            raise UpstreamError("upstream is down")
        return list(self.prompts)

    async def list_resource_templates_once(self):
        self.template_list_calls += 1
        await asyncio.sleep(self.delay_seconds)
        if self.down:
            raise UpstreamError("upstream is down")
        return list(self.templates)

    async def complete_once(self, reference, argument, context_arguments):
        self.completions.append((reference.name, argument, context_arguments))
        if self.down:
            raise UpstreamError("upstream is down")
        return Completion(values=[f"{argument['value']}-done"])

    async def read_resource(self, uri):
        self.reads.append(uri)
        if self.down:
            raise UpstreamError("upstream is down")
        if uri in self.blobs:
            mime_type, blob = self.blobs[uri]
            return SimpleNamespace(contents=[SimpleNamespace(
                text=None, blob=blob, mime_type=mime_type, meta=None)])
        resource = next((r for r in self.resources if str(r.uri) == uri), None)
        if resource is None and uri not in self.readable:
            raise UpstreamRefusal("http://upstream/mcp", -32002, f"Resource not found: {uri}")
        return SimpleNamespace(contents=[SimpleNamespace(
            text=f"contents of {uri}", blob=None,
            mime_type=resource.mime_type if resource else "text/markdown",
            meta=resource.meta if resource else None)])

    async def get_prompt(self, name, arguments):
        self.prompt_calls.append((name, arguments))
        if self.down:
            raise UpstreamError("upstream is down")
        prompt = next((p for p in self.prompts if p.name == name), None)
        if prompt is None:
            raise UpstreamRefusal("http://upstream/mcp", -32602, f"There is no prompt called {name}.")
        return GetPromptResult(description=f"{prompt.description} For {arguments}.", messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=f"{name}: {arguments}"))])


def built_against(monkeypatch, upstream, **config):
    """Build the backend as the hub does, with this stand-in as its upstream."""
    monkeypatch.setattr(proxy_module, "_upstream", lambda instance: upstream)
    return PLUGIN.build(instance(url="http://upstream/mcp", **config))


def saved_catalogue(resources=(), prompts=(), tools=UPSTREAM_TOOLS):
    return {
        "tool_catalog": json.dumps(list(tools)),
        "resource_catalog": json.dumps(list(resources)),
        "prompt_catalog": json.dumps(list(prompts)),
    }


async def test_a_resource_that_appears_upstream_after_save_is_listed_and_readable(monkeypatch):
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
    assert isinstance(server, ProxiedServer)

    uris = [str(r.uri) for r in await server.list_resources()]
    assert uris == ["story://runs/first", "story://runs/second"]

    contents = await server.read_resource("story://runs/second")
    assert contents[0].content == "contents of story://runs/second"
    assert contents[0].mime_type == "text/markdown"
    assert upstream.reads == ["story://runs/second"]


async def test_a_new_resource_is_readable_before_anyone_lists_it(monkeypatch):
    """A client that already knows the uri, from a tool's answer say, reads it."""
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
    contents = await server.read_resource("story://runs/second")
    assert contents[0].content == "contents of story://runs/second"
    assert upstream.resource_list_calls == 0, "the upstream is asked to read it, not to list"


async def test_a_resource_the_upstream_cannot_read_comes_back_with_the_upstreams_own_error(monkeypatch):
    upstream = LiveUpstream([SAVED_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
    with pytest.raises(MCPError) as raised:
        await server.read_resource("story://runs/never")
    assert (raised.value.code, raised.value.message) == (-32002, "Resource not found: story://runs/never")
    assert upstream.reads == ["story://runs/never"]


async def test_a_uri_the_upstream_reads_without_listing_it_is_read_through_the_hub(monkeypatch):
    """A pointer to a record that dropped off the upstream's list keeps working.

    The upstream reads a uri its template describes whether or not its list
    holds it now; a client behind the hub reads it the same.
    """
    unlisted = "story://runs/first/people/13"
    upstream = LiveUpstream([SAVED_RESOURCE], [], readable=[unlisted])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
    contents = await server.read_resource(unlisted)
    assert contents[0].content == f"contents of {unlisted}"


async def test_a_read_while_the_upstream_is_down_says_so_whatever_was_listed_before(monkeypatch):
    """Never "unknown resource" for an upstream nobody could reach."""
    for list_first in (False, True):
        upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [])
        server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
        if list_first:
            await server.list_resources()
        upstream.down = True
        with pytest.raises(ResourceError) as raised:
            await server.read_resource("story://runs/second")
        assert str(raised.value) == "story://runs/second: upstream is down"


async def test_upstream_down_gives_the_cached_resource_list(monkeypatch):
    upstream = LiveUpstream([], [], down=True)
    server = built_against(monkeypatch, upstream,
                           **saved_catalogue(resources=[SAVED_RESOURCE, UI_RESOURCE]))
    uris = [str(r.uri) for r in await server.list_resources()]
    assert uris == ["story://runs/first", "ui://clock/app.html"]


async def test_a_slow_upstream_gives_the_cached_resource_list_within_the_timeout(monkeypatch):
    """The backend's own timeout caps the wait, when it is the shorter of the two."""
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [], delay_seconds=5)
    upstream._cfg.timeout = 0.2  # the backend's Timeout setting, as `_upstream` reads it
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[SAVED_RESOURCE]))
    started = asyncio.get_running_loop().time()
    uris = [str(r.uri) for r in await server.list_resources()]
    assert asyncio.get_running_loop().time() - started < 2
    assert uris == ["story://runs/first"]


async def test_one_client_list_is_one_upstream_list(monkeypatch):
    upstream = LiveUpstream([SAVED_RESOURCE], [SAVED_PROMPT])
    server = built_against(monkeypatch, upstream,
                           **saved_catalogue(resources=[SAVED_RESOURCE], prompts=[SAVED_PROMPT]))
    await server.list_resources()
    await server.list_prompts()
    assert (upstream.resource_list_calls, upstream.prompt_list_calls) == (1, 1)


async def test_a_ui_resource_keeps_its_meta_listed_live_and_cached(monkeypatch):
    for down in (False, True):
        upstream = LiveUpstream([UI_RESOURCE], [], down=down)
        server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[UI_RESOURCE]))
        listed = next(r for r in await server.list_resources() if str(r.uri) == UI_RESOURCE["uri"])
        assert listed.mime_type == "text/html;profile=mcp-app"
        assert listed.meta["ui"]["csp"]["connectDomains"] == ["https://example.com"]


async def test_a_cached_ui_resource_is_read_as_it_was_before(monkeypatch):
    """Served by its mirrored entry, so the cached mime type and `_meta` go with it."""
    upstream = LiveUpstream([UI_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[UI_RESOURCE]))
    contents = await server.read_resource(UI_RESOURCE["uri"])
    assert contents[0].mime_type == "text/html;profile=mcp-app"
    assert contents[0].meta["ui"]["csp"]["connectDomains"] == ["https://example.com"]
    assert upstream.resource_list_calls == 0, "a cached resource is read without listing"


async def test_a_ui_resource_new_since_save_keeps_its_meta_when_read(monkeypatch):
    upstream = LiveUpstream([UI_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue())
    contents = await server.read_resource(UI_RESOURCE["uri"])
    assert contents[0].mime_type == "text/html;profile=mcp-app"
    assert contents[0].meta["ui"]["csp"]["connectDomains"] == ["https://example.com"]


async def test_the_tool_selection_does_not_filter_live_resources(monkeypatch):
    """No gate was on resources before, and the live list adds none."""
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [])
    server = built_against(monkeypatch, upstream, tools=["list_containers"],
                           **saved_catalogue(resources=[SAVED_RESOURCE]))
    assert len(await server.list_resources()) == 2


async def test_the_tools_path_is_unchanged(monkeypatch):
    """Tools are still the catalogue's, selected, and listing them asks no one."""
    upstream = LiveUpstream([SAVED_RESOURCE], [])
    server = built_against(monkeypatch, upstream, tools=["list_containers"], **saved_catalogue())
    assert [t.name for t in await server.list_tools()] == ["list_containers"]
    assert upstream.tool_list_calls == 0
    assert upstream.resource_list_calls == 0


async def test_a_prompt_that_appears_upstream_after_save_is_listed_and_rendered(monkeypatch):
    upstream = LiveUpstream([], [SAVED_PROMPT, NEW_PROMPT])
    server = built_against(monkeypatch, upstream, **saved_catalogue(prompts=[SAVED_PROMPT]))
    assert [p.name for p in await server.list_prompts()] == ["recap", "next_scene"]
    result = await server.get_prompt("next_scene", {"run": "the ford"})
    assert upstream.prompt_calls == [("next_scene", {"run": "the ford"})]
    assert result.messages[0].content.text == "next_scene: {'run': 'the ford'}"


async def test_upstream_down_gives_the_cached_prompt_list(monkeypatch):
    upstream = LiveUpstream([], [], down=True)
    server = built_against(monkeypatch, upstream, **saved_catalogue(prompts=[SAVED_PROMPT]))
    assert [p.name for p in await server.list_prompts()] == ["recap"]
    for name in ("recap", "next_scene"):
        with pytest.raises(MCPError) as raised:
            await server.get_prompt(name, {"run": "x"})
        assert raised.value.message == f"{name}: upstream is down", "never an unknown prompt"


async def test_a_new_resource_is_listed_and_read_through_the_protocol():
    """End to end, with the real `Upstream` talking to a real server in process.

    The upstream gains a resource after the hub's catalogue was taken, and a
    client of the hub lists it and reads it over JSON-RPC.
    """
    upstream_server = MCPServer("upstream")
    upstream_server.add_resource(FunctionResource(
        uri="story://runs/first", name="first run", mime_type="text/markdown", fn=lambda: "first"))
    async with Client(upstream_server) as upstream_client:
        upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
        upstream._session = upstream_client.session  # noqa: SLF001 - the in-process session
        catalogue = [r.model_dump(by_alias=True, exclude_none=True)
                     for r in await upstream.list_resources()]
        hub_server = ProxiedServer("hub", upstream=upstream)
        for resource in catalogue:
            proxy_module.mirror_resource(hub_server, upstream, UpstreamResource.model_validate(resource))

        upstream_server.add_resource(FunctionResource(
            uri="story://runs/second", name="second run", mime_type="text/markdown",
            fn=lambda: "second"))

        async with Client(hub_server) as hub_client:
            listed = await hub_client.list_resources()
            assert [str(r.uri) for r in listed.resources] == ["story://runs/first",
                                                              "story://runs/second"]
            read = await hub_client.read_resource("story://runs/second")
            assert read.contents[0].text == "second"
            assert read.contents[0].mime_type == "text/markdown"


class RecordingSession:
    def __init__(self, capabilities, failure=None):
        self.server_capabilities = capabilities
        self.failure = failure
        self.calls = 0

    async def list_resources(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(resources=[])


async def test_an_upstream_offering_no_resources_is_not_asked():
    from mcp.types import ServerCapabilities
    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    session = RecordingSession(ServerCapabilities())
    upstream._session = session  # noqa: SLF001
    with pytest.raises(UpstreamError, match="does not offer resources"):
        await upstream.list_resources_once()
    assert session.calls == 0


async def test_a_refusal_from_the_upstream_keeps_the_session():
    """Closing it would relaunch a stdio server on every list."""
    from mcp.shared.exceptions import MCPError
    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    session = RecordingSession(None, failure=MCPError(-32601, "Method not found"))
    upstream._session = session  # noqa: SLF001
    with pytest.raises(UpstreamError):
        await upstream.list_resources_once()
    assert upstream._session is session  # noqa: SLF001
    assert session.calls == 1, "asked once, never retried"


async def test_a_transport_failure_closes_the_session_and_is_not_retried():
    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    session = RecordingSession(None, failure=ConnectionError("gone"))
    upstream._session = session  # noqa: SLF001
    with pytest.raises(UpstreamError):
        await upstream.list_resources_once()
    assert upstream._session is None  # noqa: SLF001
    assert session.calls == 1


# ── what a review of the live lists found ───────────────────────────────────

async def test_a_cached_ui_resource_is_listed_as_it_is_read(monkeypatch):
    """The policy a client is offered is the one it is served, the saved one."""
    changed = {**UI_RESOURCE, "name": "app renamed", "mimeType": "text/html",
               "_meta": {"ui": {"csp": {"connectDomains": ["https://elsewhere.example"]}}}}
    upstream = LiveUpstream([changed], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(resources=[UI_RESOURCE]))
    listed = (await server.list_resources())[0]
    read = (await server.read_resource(UI_RESOURCE["uri"]))[0]
    assert (listed.mime_type, read.mime_type) == ("text/html;profile=mcp-app",) * 2
    assert listed.meta == read.meta == UI_RESOURCE["_meta"]
    assert listed.name == "clock app"


async def test_a_record_the_upstream_lists_twice_is_offered_once(monkeypatch):
    upstream = LiveUpstream([NEW_RESOURCE, {**NEW_RESOURCE, "name": "again"}],
                            [NEW_PROMPT, {**NEW_PROMPT, "description": "again"}],
                            templates=[{"uriTemplate": "story://runs/{run}", "name": "run"}] * 2)
    server = built_against(monkeypatch, upstream, **saved_catalogue())
    assert [(str(r.uri), r.name) for r in await server.list_resources()] == [
        ("story://runs/second", "second run")]
    assert [(p.name, p.description) for p in await server.list_prompts()] == [
        ("next_scene", "Plan the next scene.")]
    assert len(await server.list_resource_templates()) == 1


async def test_a_record_handed_on_is_a_copy(monkeypatch):
    upstream = LiveUpstream([NEW_RESOURCE], [NEW_PROMPT],
                            templates=[{"uriTemplate": "story://runs/{run}", "name": "run"}])
    server = built_against(monkeypatch, upstream, **saved_catalogue())
    (await server.list_resources())[0].name = "changed by a reader"
    (await server.list_prompts())[0].name = "changed by a reader"
    (await server.list_resource_templates())[0].name = "changed by a reader"
    assert upstream.resources[0].name == "second run"
    assert upstream.prompts[0].name == "next_scene"
    assert upstream.templates[0].name == "run"


async def test_after_a_list_with_no_answer_the_cache_answers_without_asking(monkeypatch):
    """A slow start is waited out once, not once per list."""
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [SAVED_PROMPT, NEW_PROMPT],
                            delay_seconds=5)
    upstream._cfg.timeout = 0.2
    server = built_against(monkeypatch, upstream, **saved_catalogue(
        resources=[SAVED_RESOURCE], prompts=[SAVED_PROMPT]))
    started = asyncio.get_running_loop().time()
    assert [str(r.uri) for r in await server.list_resources()] == ["story://runs/first"]
    assert [p.name for p in await server.list_prompts()] == ["recap"]
    assert asyncio.get_running_loop().time() - started < 0.5
    assert (upstream.resource_list_calls, upstream.prompt_list_calls) == (1, 0)


async def test_the_upstream_is_asked_again_once_the_pause_is_over(monkeypatch):
    upstream = LiveUpstream([SAVED_RESOURCE, NEW_RESOURCE], [], down=True)
    monkeypatch.setattr(proxy_module, "_upstream", lambda instance: upstream)
    server = ProxiedServer("hub", upstream=upstream, live_list_pause_seconds=0.05)
    assert await server.list_resources() == []
    upstream.down = False
    assert await server.list_resources() == []
    await asyncio.sleep(0.1)
    assert len(await server.list_resources()) == 2
    assert upstream.resource_list_calls == 2


async def test_a_refusal_is_an_answer_and_starts_no_pause(monkeypatch):
    class Refusing(LiveUpstream):
        async def list_prompts_once(self):
            self.prompt_list_calls += 1
            raise UpstreamRefusal("http://upstream/mcp", -32601, "The server does not offer prompts.")

    upstream = Refusing([SAVED_RESOURCE], [])
    server = built_against(monkeypatch, upstream, **saved_catalogue(prompts=[SAVED_PROMPT]))
    assert [p.name for p in await server.list_prompts()] == ["recap"]
    assert len(await server.list_resources()) == 1
    assert upstream.resource_list_calls == 1


async def test_a_binary_resource_new_since_save_is_served_as_a_blob(monkeypatch):
    import base64
    picture = b"\x89PNG\r\n\x1a\n not text"
    uri = "story://runs/first/map.png"
    upstream = LiveUpstream([], [], blobs={uri: ("image/png", base64.b64encode(picture).decode())})
    server = built_against(monkeypatch, upstream, **saved_catalogue())
    contents = await server.read_resource(uri)
    assert contents[0].content == picture
    assert contents[0].mime_type == "image/png"


async def test_resource_templates_are_listed_live_and_empty_when_the_upstream_is_down(monkeypatch):
    template = {"uriTemplate": "story://runs/{run}/people/{entry}", "name": "person"}
    upstream = LiveUpstream([], [], templates=[template])
    server = built_against(monkeypatch, upstream, **saved_catalogue())
    assert [t.uri_template for t in await server.list_resource_templates()] == [template["uriTemplate"]]
    down = LiveUpstream([], [], templates=[template], down=True)
    server = built_against(monkeypatch, down, **saved_catalogue())
    assert await server.list_resource_templates() == []


async def test_a_cached_prompt_is_rendered_with_the_upstreams_own_description(monkeypatch):
    upstream = LiveUpstream([], [SAVED_PROMPT])
    server = built_against(monkeypatch, upstream, **saved_catalogue(prompts=[SAVED_PROMPT]))
    result = await server.get_prompt("recap", {"run": "the ford"})
    assert result.description == "Recap a run. For {'run': 'the ford'}."
    assert upstream.prompt_calls == [("recap", {"run": "the ford"})]


async def test_a_completion_is_asked_of_the_upstream(monkeypatch):
    from mcp.types import CompletionArgument, CompletionContext, PromptReference
    upstream = LiveUpstream([], [SAVED_PROMPT])
    server = built_against(monkeypatch, upstream, **saved_catalogue(prompts=[SAVED_PROMPT]))
    completion = await server._complete(  # noqa: SLF001
        PromptReference(type="ref/prompt", name="recap"), CompletionArgument(name="run", value="the-f"),
        CompletionContext(arguments={"place": "ford"}))
    assert completion.values == ["the-f-done"]
    assert upstream.completions == [("recap", {"name": "run", "value": "the-f"}, {"place": "ford"})]
    upstream.down = True
    assert await server._complete(  # noqa: SLF001
        PromptReference(type="ref/prompt", name="recap"), CompletionArgument(name="run", value="x"),
        None) is None


async def test_a_refusal_templates_and_completion_reach_a_client_over_the_protocol():
    """End to end with the real `Upstream`: the upstream's own refusal, its
    templates, a read its template answers, and its completions."""
    from mcp.server.mcpserver.prompts import Prompt as ServerPrompt
    from mcp.types import INVALID_PARAMS, Completion as ServerCompletion

    upstream_server = MCPServer("upstream")

    async def continue_run(run: str) -> str:
        if run != "the-ford":
            raise MCPError(INVALID_PARAMS, f'There is no run called "{run}".')
        return f"Continue {run}."

    upstream_server.add_prompt(ServerPrompt.from_function(continue_run, name="continue_run",
                                                          description="Continue a run."))

    @upstream_server.resource("story://runs/{run}/people/{entry}", name="person")
    def person(run: str, entry: str) -> str:
        return f"{entry} of {run}"

    @upstream_server.completion()
    async def complete(reference, argument, context):
        return ServerCompletion(values=[name for name in ("the-ford", "the-mill")
                                        if name.startswith(argument.value)])

    async with Client(upstream_server) as upstream_client:
        upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
        upstream._session = upstream_client.session  # noqa: SLF001 - the in-process session
        catalogue = await upstream.list_prompts()
        hub_server = ProxiedServer("hub", upstream=upstream)
        for prompt in catalogue:
            proxy_module.mirror_prompt(hub_server, upstream, prompt)

        async with Client(hub_server) as hub_client:
            with pytest.raises(MCPError) as raised:
                await hub_client.get_prompt("continue_run", {"run": "no-such-run"})
            assert (raised.value.code, raised.value.message) == (
                INVALID_PARAMS, 'There is no run called "no-such-run".')
            assert upstream._session is upstream_client.session, "a refusal keeps the session"  # noqa: SLF001

            templates = await hub_client.list_resource_templates()
            assert [t.uri_template for t in templates.resource_templates] == [
                "story://runs/{run}/people/{entry}"]
            read = await hub_client.read_resource("story://runs/the-ford/people/baker")
            assert read.contents[0].text == "baker of the-ford"

            from mcp.types import PromptReference
            completed = await hub_client.complete(
                PromptReference(type="ref/prompt", name="continue_run"), {"name": "run", "value": "the-f"})
            assert completed.completion.values == ["the-ford"]


class RefusingOnce:
    def __init__(self, failure):
        self.server_capabilities = None
        self.failure = failure
        self.calls = 0

    async def get_prompt(self, name, arguments):
        self.calls += 1
        raise self.failure


async def test_a_refusal_through_the_retrying_path_keeps_the_session_and_is_asked_once():
    from mcp.shared.exceptions import MCPError as ClientError
    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    session = RefusingOnce(ClientError(-32602, "There is no such run."))
    upstream._session = session  # noqa: SLF001
    with pytest.raises(UpstreamRefusal) as raised:
        await upstream.get_prompt("continue_run", {"run": "x"})
    assert (raised.value.code, raised.value.message) == (-32602, "There is no such run.")
    assert upstream._session is session  # noqa: SLF001
    assert session.calls == 1


async def test_a_dropped_connection_is_still_not_a_refusal():
    from mcp.shared.exceptions import MCPError as ClientError
    from mcp.types import CONNECTION_CLOSED
    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    session = RecordingSession(None, failure=ClientError(CONNECTION_CLOSED, "Connection closed"))
    upstream._session = session  # noqa: SLF001
    with pytest.raises(UpstreamError) as raised:
        await upstream.list_resources_once()
    assert not isinstance(raised.value, UpstreamRefusal)
    assert upstream._session is None  # noqa: SLF001


async def test_an_upstream_offering_prompts_and_no_resources_is_asked_for_its_prompts():
    from mcp.types import PromptsCapability, ServerCapabilities

    class PromptsOnly:
        server_capabilities = ServerCapabilities(prompts=PromptsCapability())

        async def list_prompts(self):
            return SimpleNamespace(prompts=["asked"])

    upstream = Upstream(UpstreamConfig(url="http://upstream/mcp"))
    upstream._session = PromptsOnly()  # noqa: SLF001
    assert await upstream.list_prompts_once() == ["asked"]


class Failing:
    def __init__(self, failure):
        self.failure = failure

    async def read_resource(self, uri):
        raise self.failure

    async def get_prompt(self, name, arguments):
        raise self.failure


async def test_a_mirrored_entry_hands_on_the_upstreams_refusal_and_says_when_it_is_down():
    from mcp.types import Prompt as UpstreamPromptRecord
    refusal = UpstreamRefusal("http://upstream/mcp", -32602, "There is no run called x.")
    for failure, expected in ((refusal, MCPError), (UpstreamError("upstream is down"), ResourceError)):
        server = MCPServer("t")
        proxy_module.mirror_resource(server, Failing(failure),
                                     UpstreamResource.model_validate(SAVED_RESOURCE))
        with pytest.raises(expected) as raised:
            await server.read_resource(SAVED_RESOURCE["uri"])
        assert str(raised.value).endswith(
            "There is no run called x." if expected is MCPError else "upstream is down")
    server = MCPServer("t")
    proxy_module.mirror_prompt(server, Failing(refusal), UpstreamPromptRecord.model_validate(SAVED_PROMPT))
    with pytest.raises(MCPError) as raised:
        await server.get_prompt("recap", {})
    assert (raised.value.code, raised.value.message) == (-32602, "There is no run called x.")
