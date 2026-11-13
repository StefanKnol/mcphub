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
