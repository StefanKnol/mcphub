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


def test_unrepresentable_parameter_names_are_reported_not_silently_dropped():
    sig, _, skipped = _build_signature({
        "type": "object",
        "properties": {"fine": {"type": "string"}, "not-an-identifier": {"type": "string"}, "class": {"type": "string"}},
    })
    assert list(sig.parameters) == ["fine"]
    assert set(skipped) == {"not-an-identifier", "class"}


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
