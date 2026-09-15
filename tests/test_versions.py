"""Per-account version pinning, and the background update check.

Accounts may sit on different versions of one backend. That is not a
configuration nicety: a pinned version can offer a different set of tools —
0.14.8.0 of one real server has 174, 0.15.0.0 has 182 — so each version needs
its own server, and a request routes to the one belonging to whoever is asking.
"""

import json

import pytest

from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.mcpproxy import PLUGIN
from mcphub.registry import MAX_LIMIT, PackageRef, _sortable
from mcphub.updates import MIN_INTERVAL, UpdateChecker

PACKAGE = {"registryType": "pypi", "identifier": "mikrotik-mcp", "runtime": "uvx", "args": []}
CATALOG = json.dumps([{"name": "t", "description": "d",
                       "inputSchema": {"type": "object", "properties": {}}}])


def instance(**config) -> BackendInstance:
    return BackendInstance(slug="s", title="t", plugin_id="mcp-proxy", config=config)


# ── pin syntax ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,runtime,pinned", [
    ("pypi", "uvx", "uvx pkg==1.2.3"),
    ("npm", "npx", "npx pkg@1.2.3"),
    ("oci", "docker", "docker pkg:1.2.3"),
])
def test_each_registry_spells_a_pin_differently(kind, runtime, pinned):
    """`uvx pkg@1.2.3` is not an error; it asks for a differently named package."""
    assert PackageRef(kind, "pkg", runtime).command("1.2.3") == pinned


def test_runtime_arguments_stay_before_the_package():
    ref = PackageRef("npm", "pkg", "npx", ("-y",))
    assert ref.command("2.0.0") == "npx -y pkg@2.0.0"


def test_no_version_gives_the_plain_command():
    assert PackageRef("pypi", "pkg", "uvx").command() == "uvx pkg"


# ── version ordering ──────────────────────────────────────────────────────

def test_versions_sort_numerically_not_as_text():
    """0.14.10.0 is newer than 0.14.9.0, which a string sort gets backwards."""
    ordered = sorted(["0.14.9.0", "0.14.10.0", "0.15.0.0"], key=_sortable, reverse=True)
    assert ordered == ["0.15.0.0", "0.14.10.0", "0.14.9.0"]


def test_non_numeric_parts_do_not_crash_the_sort():
    assert sorted(["1.0.0", "1.0.0-rc1", "2.0"], key=_sortable, reverse=True)[0] == "2.0"


def test_registry_limit_is_within_what_it_accepts():
    """Above this the registry answers 422 rather than clamping."""
    assert MAX_LIMIT == 100


# ── building a variant ────────────────────────────────────────────────────

def test_a_variant_pins_the_command_and_swaps_the_catalogue():
    got = PLUGIN.variant(instance(
        command="uvx mikrotik-mcp", registry_package=PACKAGE,
        version_catalogs={"0.1.0": {"tools": CATALOG, "version": "0.1.0"}},
    ), "0.1.0")
    assert got.config["command"] == "uvx mikrotik-mcp==0.1.0"
    assert got.config["tool_catalog"] == CATALOG
    assert got.config["upstream_version"] == "0.1.0"


def test_no_pin_returns_the_backend_untouched():
    original = instance(command="uvx pkg", registry_package=PACKAGE)
    assert PLUGIN.variant(original, "") is original


def test_a_hand_written_command_is_left_alone():
    """Guessing which token of an arbitrary command is the package would
    eventually rewrite the wrong one."""
    got = PLUGIN.variant(instance(command="uvx thing --root /data"), "9.9")
    assert got.config["command"] == "uvx thing --root /data"


def test_a_version_with_no_catalogue_yields_no_tools_rather_than_wrong_ones():
    got = PLUGIN.variant(instance(command="uvx pkg", registry_package=PACKAGE,
                                  version_catalogs={}), "0.2.0")
    assert PLUGIN.tool_names(got) == set()


def test_secrets_travel_to_the_variant():
    """A pinned version still needs the credentials the backend was given."""
    original = BackendInstance(slug="s", title="t", plugin_id="mcp-proxy",
                               config={"command": "uvx pkg", "registry_package": PACKAGE},
                               secrets={"env_TOKEN": "abc"})
    assert PLUGIN.variant(original, "1.0.0").secrets == {"env_TOKEN": "abc"}


# ── the update check ──────────────────────────────────────────────────────

def test_interval_has_a_floor():
    """So a mistyped interval cannot become a hot loop against the registry."""
    assert UpdateChecker(None, 5)._interval == MIN_INTERVAL


def test_a_sensible_interval_is_kept():
    assert UpdateChecker(None, 3600)._interval == 3600


async def test_check_all_skips_backends_that_did_not_come_from_the_registry():
    class Hub:
        db = None
        def backend_rows(self):
            return [{"id": 1, "slug": "manual", "config_json": json.dumps({"command": "uvx x"})}]

    assert await UpdateChecker(Hub(), 3600).check_all() == 0


async def test_check_all_survives_a_corrupt_row():
    class Hub:
        db = None
        def backend_rows(self):
            return [{"id": 1, "slug": "bad", "config_json": "{not json"}]

    assert await UpdateChecker(Hub(), 3600).check_all() == 0
