"""End-to-end tool behaviour against a stand-in RouterOS.

Exercises the tools the way a client does — through the MCP server's dispatch —
so the argument schemas and the id handling are both covered.
"""

import copy
import json

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcphub.plugins.builtin.mikrotik.client import _ros_values
from mcphub.plugins.builtin.mikrotik.tools import register


class FakeRouter:
    """Mimics the parts of RouterOS the client layer uses.

    Crucially it behaves like the *binary API*: every item carries a real `.id`,
    and writes address items by that id alone. A positional index reaching this
    layer simply finds nothing, which is what makes the tests meaningful.
    """

    def __init__(self, rules):
        # Deep copy: a shallow one shares the rule dicts with the module-level
        # fixture, so an update in one test leaks into every later test.
        self._items = {("ip", "firewall", "filter"): copy.deepcopy(rules)}
        self.calls = []

    async def list(self, *path):
        return [dict(r) for r in self._items.get(path, [])]

    async def get(self, *path, item_id):
        return next((dict(r) for r in self._items.get(path, []) if r[".id"] == item_id), None)

    async def add(self, *path, **fields):
        items = self._items.setdefault(path, [])
        new_id = f"*{len(items) + 100:x}"
        items.append({".id": new_id, **_ros_values(fields)})
        return new_id

    async def update(self, *path, item_id, **fields):
        self.calls.append(("update", item_id))
        for row in self._items.get(path, []):
            if row[".id"] == item_id:
                # Same translation the real client applies, so the stored shape
                # matches the device's (booleans become "yes"/"no").
                row.update(_ros_values(fields))
                return
        raise AssertionError(f"update targeted {item_id!r}, which does not exist")

    async def remove(self, *path, item_id):
        self.calls.append(("remove", item_id))
        items = self._items.get(path, [])
        before = len(items)
        self._items[path] = [r for r in items if r[".id"] != item_id]
        if len(self._items[path]) == before:
            raise AssertionError(f"remove targeted {item_id!r}, which does not exist")

    async def run(self, *path, command, **fields):
        self.calls.append((command, fields))
        return []


RULES = [
    {".id": "*0", "chain": "input", "action": "accept", "comment": "back-to-home-vpn", "dynamic": "true"},
    {".id": "*7", "chain": "input", "action": "drop", "src-address": "156.147.69.32"},
    {".id": "*a", "chain": "forward", "action": "accept", "comment": "Accept Port Forwards"},
    {".id": "*1f", "chain": "input", "action": "accept", "comment": "Accept Existing Connections (CRITICAL)"},
]


@pytest.fixture
def server():
    router = FakeRouter(RULES)
    mcp = MCPServer("test-router")
    register(mcp, router, title="Test Router")
    return mcp, router


async def call(mcp, name, **args):
    """Call a tool the way a client does, turning any failure into ToolFailed."""
    try:
        result = await mcp.call_tool(name, args)
    except ToolError as exc:
        raise ToolFailed(str(exc)) from exc
    if result.is_error:
        raise ToolFailed(result.content[0].text)
    return json.loads(result.content[0].text)


class ToolFailed(Exception):
    """A tool that reported is_error, so tests can assert on the message."""


async def test_list_reports_ids_and_positions(server):
    mcp, _ = server
    out = await call(mcp, "list_firewall_rules")
    ids = [r["id"] for r in out["rules"]]
    assert ids == ["*7", "*a", "*1f"], "dynamic rules are excluded by default"
    # Positions stay the device's real evaluation positions: the dynamic rule
    # filtered out of this view still occupies position 0 on the router, and
    # renumbering the visible rules 0,1,2 would misreport where they sit.
    assert [r["position"] for r in out["rules"]] == [1, 2, 3]


async def test_list_can_include_dynamic_rules(server):
    mcp, _ = server
    out = await call(mcp, "list_firewall_rules", include_dynamic=True)
    assert [r["id"] for r in out["rules"]] == ["*0", "*7", "*a", "*1f"]


async def test_update_by_real_id_succeeds_and_reads_back(server):
    """The exact operation that failed on the server this replaces."""
    mcp, router = server
    out = await call(mcp, "update_firewall_rule", rule_id="*a", comment="renamed")
    assert out["updated"] is True
    assert out["before"]["comment"] == "Accept Port Forwards"
    assert out["after"]["comment"] == "renamed"
    assert ("update", "*a") in router.calls


async def test_update_by_position_is_refused_before_touching_the_device(server):
    mcp, router = server
    # "1" is the position of *a in the listing above — the value a caller would
    # copy from CLI-style output.
    with pytest.raises(ToolFailed) as excinfo:
        await call(mcp, "update_firewall_rule", rule_id="1", comment="oops")
    assert "position" in str(excinfo.value).lower()
    assert router.calls == [], "nothing should have been sent to the device"


async def test_update_of_a_removed_rule_fails_cleanly(server):
    mcp, router = server
    await call(mcp, "remove_firewall_rule", rule_id="*7")
    with pytest.raises(ToolFailed) as excinfo:
        await call(mcp, "update_firewall_rule", rule_id="*7", comment="x")
    assert "*7" in str(excinfo.value)
    # It failed by name rather than silently editing whatever took that slot.
    assert ("update", "*7") not in router.calls


async def test_remove_returns_the_deleted_rule(server):
    mcp, _ = server
    out = await call(mcp, "remove_firewall_rule", rule_id="*a")
    assert out["removed"] is True
    assert out["deleted_rule"]["comment"] == "Accept Port Forwards"
    remaining = await call(mcp, "list_firewall_rules")
    assert "*a" not in [r["id"] for r in remaining["rules"]]


async def test_remove_honours_the_comment_guard(server):
    mcp, router = server
    with pytest.raises(ToolFailed) as excinfo:
        await call(mcp, "remove_firewall_rule", rule_id="*1f", confirm_comment="Accept Port Forwards")
    assert "refusing" in str(excinfo.value).lower()
    assert router.calls == []
    still_there = await call(mcp, "get_firewall_rule", rule_id="*1f")
    assert still_there["comment"] == "Accept Existing Connections (CRITICAL)"


async def test_disable_keeps_the_rule_in_place(server):
    mcp, _ = server
    out = await call(mcp, "set_firewall_rule_enabled", rule_id="*7", enabled=False)
    assert out["enabled"] is False
    assert out["rule"]["disabled"] == "yes"


async def test_add_returns_the_created_rule(server):
    mcp, _ = server
    out = await call(mcp, "add_firewall_rule", chain="forward", action="drop",
                     comment="block thing", dst_port="853", protocol="udp")
    assert out["created"] is True
    assert out["id"].startswith("*")
    assert out["rule"]["comment"] == "block thing"
    assert out["rule"]["dst-port"] == "853"


async def test_add_with_place_before_moves_by_id(server):
    mcp, router = server
    out = await call(mcp, "add_firewall_rule", chain="input", action="drop", place_before="*1f")
    assert out["placed_before"] == "*1f"
    move = next(c for c in router.calls if c[0] == "move")
    assert move[1]["destination"] == "*1f"
    assert move[1]["numbers"] == out["id"]


async def test_place_before_rejects_a_position(server):
    mcp, router = server
    with pytest.raises(ToolFailed):
        await call(mcp, "add_firewall_rule", chain="input", action="drop", place_before="2")
    assert not any(c[0] == "move" for c in router.calls)
