"""The bug this project exists to not repeat.

The server this replaces listed firewall rules by *position* and then looked
them up for writing with `where .id=<position>`, a predicate that can never
match, so every write failed with "not found" on rules that plainly existed.
Had it matched, it would have been worse: positions shift, so the write would
have landed on a different rule than the caller meant.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcphub.plugins.builtin.mikrotik.tools import _require_real_id, _summarise


@pytest.mark.parametrize("value", ["*1", "*a", "*1f", "  *7  "])
def test_real_ids_are_accepted(value):
    assert _require_real_id(value) == value.strip()


@pytest.mark.parametrize("value", ["0", "3", "13", "", "position-3"])
def test_positional_indices_are_rejected(value):
    with pytest.raises(ToolError) as excinfo:
        _require_real_id(value)
    # ToolError specifically: the SDK replaces the text of any other exception
    # with a generic "Error executing tool", and this message is the whole point
    # — the model reads it and retries with the right identifier.
    assert "id" in str(excinfo.value).lower()
    assert "position" in str(excinfo.value).lower()


def test_summarise_exposes_id_and_position_separately():
    rows = [
        {".id": "*a", "chain": "input", "action": "accept", "bytes": "999"},
        {".id": "*3", "chain": "forward", "action": "drop", "bytes": "1"},
    ]
    out = _summarise(rows, (".id", "chain", "action"), verbose=False)

    assert [r["id"] for r in out] == ["*a", "*3"]
    assert [r["position"] for r in out] == [0, 1]
    # Position is not the id, and nothing in the payload invites confusing them.
    assert out[0]["id"] != out[0]["position"]
    assert "bytes" not in out[0], "non-summary fields should be dropped unless verbose"


def test_summarise_verbose_keeps_everything():
    rows = [{".id": "*a", "chain": "input", "bytes": "999"}]
    out = _summarise(rows, (".id", "chain"), verbose=True)
    assert out[0]["bytes"] == "999"


def test_a_position_that_looks_like_an_id_is_still_rejected():
    """The exact upstream failure: taking `3` from list output into a write."""
    listed = _summarise([{".id": "*f", "chain": "forward"}], (".id", "chain"), False)
    position = listed[0]["position"]
    with pytest.raises(ToolError):
        _require_real_id(str(position))
    assert _require_real_id(listed[0]["id"]) == "*f"
