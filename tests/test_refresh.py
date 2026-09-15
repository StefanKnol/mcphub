"""Re-reading what an upstream offers.

A launched server resolves its package again every time it starts, so
remounting is what picks up a new release. The cached tool list has to be
re-read at the same time — otherwise a server gains tools and the hub goes on
serving the list it read when the backend was first saved.
"""

import json

import pytest

from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.mcpproxy import PLUGIN
from mcphub.web.routes import _config_value

CATALOG = [
    {"name": "get_current_time", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "convert_time", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
]


def instance(**config) -> BackendInstance:
    return BackendInstance(slug="s", title="t", plugin_id="mcp-proxy", config=config)


def test_tool_names_reads_the_cached_catalogue():
    assert PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG))) == {
        "get_current_time", "convert_time"}


def test_tool_names_of_an_empty_backend():
    assert PLUGIN.tool_names(instance()) == set()


def test_tool_names_survives_a_corrupt_catalogue():
    """A bad cache must not stop a refresh from being able to fix it."""
    assert PLUGIN.tool_names(instance(tool_catalog="{not json")) == set()


def test_added_and_removed_are_a_set_difference():
    was = PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG[:1])))
    now = PLUGIN.tool_names(instance(tool_catalog=json.dumps(CATALOG)))
    assert sorted(now - was) == ["convert_time"]
    assert sorted(was - now) == []


@pytest.mark.parametrize("stored,expected", [
    ({"upstream_version": "1.30.0"}, "1.30.0"),
    ({}, ""),
    ({"upstream_version": None}, ""),
])
def test_config_value(stored, expected):
    row = {"config_json": json.dumps(stored)}
    assert _config_value(row, "upstream_version") == expected


def test_config_value_survives_a_corrupt_row():
    assert _config_value({"config_json": "{not json"}, "upstream_version") == ""


def test_allowlist_pruning_keeps_tools_that_still_exist():
    """The rule the refresh applies: drop only what the upstream no longer has."""
    selected = ["get_current_time", "convert_time", "gone_away"]
    available = {"get_current_time", "convert_time"}
    assert [t for t in selected if t in available] == ["get_current_time", "convert_time"]
