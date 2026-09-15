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
