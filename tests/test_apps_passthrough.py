"""MCP Apps survives the proxy.

An Apps-enabled tool carries `_meta.ui.resourceUri` pointing at a `ui://`
resource the host renders in a sandboxed iframe. A proxy that forwards tools
but drops their `_meta`, or forwards `_meta` but not the resource, hands the
client either a tool that has quietly lost its interface or one pointing at
something it cannot fetch. Neither fails loudly.
"""

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import Resource as UpstreamResource
from mcp.types import Tool as UpstreamTool

from mcphub.plugins.builtin.mcpproxy.mirror import mirror_resource, mirror_tool

UI_URI = "ui://clock/app.html"

APPS_TOOL = {
    "name": "show_clock",
    "description": "Display a clock.",
    "inputSchema": {"type": "object", "properties": {"tz": {"type": "string"}}},
    "_meta": {"ui": {"resourceUri": UI_URI}},
}

UI_RESOURCE = {
    "uri": UI_URI,
    "name": "clock app",
    "mimeType": "text/html;profile=mcp-app",
    "_meta": {"ui": {"csp": {"connectDomains": ["https://example.com"]}}},
}


class FakeUpstream:
    def __init__(self):
        self.reads = []

    async def call_tool(self, name, arguments):
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": "ok"})()],
                              "is_error": False, "structured_content": None})()

    async def read_resource(self, uri):
        self.reads.append(uri)
        text = "<!doctype html><p>clock</p>"
        return type("R", (), {"contents": [type("C", (), {"text": text, "blob": None})()]})()


@pytest.fixture
def mirrored():
    mcp, up = MCPServer("t"), FakeUpstream()
    assert mirror_tool(mcp, up, UpstreamTool.model_validate(APPS_TOOL))
    assert mirror_resource(mcp, up, UpstreamResource.model_validate(UI_RESOURCE))
    return mcp, up


async def test_tool_meta_survives_the_proxy(mirrored):
    mcp, _ = mirrored
    tool = (await mcp.list_tools())[0]
    assert tool.meta is not None, "the Apps binding was dropped"
    assert tool.meta["ui"]["resourceUri"] == UI_URI


async def test_the_ui_resource_is_exposed(mirrored):
    mcp, _ = mirrored
    uris = [str(r.uri) for r in await mcp.list_resources()]
    assert UI_URI in uris, "the tool would point at a resource the client cannot fetch"


async def test_the_resource_keeps_its_mime_type(mirrored):
    """`text/html;profile=mcp-app` is how a host knows to render it as an app."""
    mcp, _ = mirrored
    resource = next(r for r in await mcp.list_resources() if str(r.uri) == UI_URI)
    assert resource.mime_type == "text/html;profile=mcp-app"


async def test_the_resource_keeps_its_csp_metadata(mirrored):
    """Without `_meta.ui`, the host has no sandbox policy to apply."""
    mcp, _ = mirrored
    resource = next(r for r in await mcp.list_resources() if str(r.uri) == UI_URI)
    assert resource.meta is not None
    assert resource.meta["ui"]["csp"]["connectDomains"] == ["https://example.com"]


async def test_contents_are_read_through_on_demand(mirrored):
    mcp, up = mirrored
    assert up.reads == [], "contents should not be fetched until read"
    result = await mcp.read_resource(UI_URI)
    body = result[0].content if isinstance(result, list) else result
    assert "clock" in str(body)
    assert up.reads == [UI_URI]


async def test_a_tool_without_apps_metadata_is_unaffected():
    mcp = MCPServer("t")
    mirror_tool(mcp, FakeUpstream(), UpstreamTool.model_validate(
        {"name": "plain", "description": "d", "inputSchema": {"type": "object", "properties": {}}}))
    assert (await mcp.list_tools())[0].meta is None
