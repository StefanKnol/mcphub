"""Re-attaching a backend to the registry entry its command launches.

Two backends arrive here. One added from the registry before saving preserved
config, whose metadata a save discarded; and one added by hand, which never had
any. Both show a freeform environment box carrying an example about someone
else's API key, instead of the variables their server actually declares.
"""

import pytest

from mcphub.registry import package_from_command
from mcphub.web.routes import _split_env_blob


@pytest.mark.parametrize("command,expected", [
    ("uvx mikrotik-mcp", ("uvx", "mikrotik-mcp")),
    ("uvx mikrotik-mcp==0.1.0", ("uvx", "mikrotik-mcp")),
    ("npx -y @scope/server", ("npx", "@scope/server")),
    ("npx -y @scope/server@1.2.3", ("npx", "@scope/server")),
    ("npx -y plain@2.0.0", ("npx", "plain")),
    ("/usr/local/bin/uvx some-server", ("uvx", "some-server")),
])
def test_package_is_read_from_the_command(command, expected):
    assert package_from_command(command) == expected


def test_a_scoped_name_keeps_its_leading_at_sign():
    """`@scope/pkg` must not be mistaken for a pin and truncated to nothing."""
    assert package_from_command("npx -y @modelcontextprotocol/server-time")[1] == \
        "@modelcontextprotocol/server-time"


@pytest.mark.parametrize("command", ["docker run thing", "uvx", "", "./my-server --flag"])
def test_an_unreadable_command_yields_nothing(command):
    """Better no match than a wrong one: adopting the wrong entry would attach
    another server's variables to this backend."""
    assert package_from_command(command) == ("", "")


# ── migrating the freeform block ──────────────────────────────────────────

def test_declared_variables_move_out_of_the_block():
    moved, leftover = _split_env_blob("A=1\nB=2", ["A"])
    assert moved == {"A": "1"}
    assert leftover == "B=2"


def test_undeclared_variables_stay():
    """A server does not declare everything someone might want to pass it."""
    moved, leftover = _split_env_blob("CUSTOM=keepme", ["MIKROTIK_HOST"])
    assert moved == {}
    assert leftover == "CUSTOM=keepme"


def test_an_empty_block_is_handled():
    assert _split_env_blob("", ["A"]) == ({}, "")


def test_everything_declared_empties_the_block():
    moved, leftover = _split_env_blob("A=1\nB=2", ["A", "B"])
    assert moved == {"A": "1", "B": "2"}
    assert leftover == ""


def test_values_containing_equals_survive():
    moved, _ = _split_env_blob("TOKEN=abc=def", ["TOKEN"])
    assert moved == {"TOKEN": "abc=def"}


def test_nothing_is_lost_in_the_move():
    """The point of the migration is that the server still gets the same env."""
    blob = "A=1\nB=2\nC=3"
    moved, leftover = _split_env_blob(blob, ["A", "C"])
    recombined = {**moved, **dict(
        line.split("=", 1) for line in leftover.splitlines() if "=" in line)}
    assert recombined == {"A": "1", "B": "2", "C": "3"}


# ── when a relink is attempted ────────────────────────────────────────────

import json  # noqa: E402

from mcphub.web.routes import LINK_ATTEMPTED_KEY, needs_relink  # noqa: E402


def row(**config):
    return {"slug": "s", "config_json": json.dumps(config)}


def test_a_backend_with_a_command_and_no_metadata_needs_one():
    assert needs_relink(row(command="uvx some-server"))


def test_a_linked_backend_does_not():
    assert not needs_relink(row(command="uvx x", registry_name="a/b",
                                registry_package={"identifier": "x"}))


def test_an_already_attempted_backend_is_not_retried():
    """A command that is not in the registry must not be looked up on every
    page load, forever."""
    assert not needs_relink(row(command="uvx nowhere", **{LINK_ATTEMPTED_KEY: "2026-01-01"}))


def test_a_backend_with_no_command_has_nothing_to_look_up():
    assert not needs_relink(row(url="http://x/mcp"))


def test_a_corrupt_row_is_skipped_rather_than_raising():
    assert not needs_relink({"slug": "s", "config_json": "{not json"})


def test_half_linked_counts_as_unlinked():
    """A name without a package reference cannot produce a pinnable backend."""
    assert needs_relink(row(command="uvx x", registry_name="a/b"))
