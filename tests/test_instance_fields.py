"""A backend's settings form is shaped by that backend.

A proxy backend added from the registry used to lose everything the registry
knew about it the moment it was saved: seven described, typed variables
collapsed into one freeform blob whose placeholder advertised some other
server's API key.
"""

import pytest

from mcphub.plugins.base import BackendInstance, ConfigField, choice_pairs
from mcphub.plugins.builtin.mcpproxy import (
    CONNECTION_KEY,
    ENV_PREFIX,
    PLUGIN,
    _collect_env,
    _upstream,
)

DECLARED = [
    {"name": "MIKROTIK_HOST", "description": "Router address", "isRequired": True},
    {"name": "MIKROTIK_PASSWORD", "description": "Password", "isRequired": True, "isSecret": True},
]


def instance(**config) -> BackendInstance:
    secrets = config.pop("_secrets", {})
    return BackendInstance(slug="s", title="t", plugin_id="mcp-proxy", config=config, secrets=secrets)


# ── form shaping ──────────────────────────────────────────────────────────

def test_generic_backend_gets_the_generic_form():
    keys = [f.key for f in PLUGIN.fields_for(None)]
    assert "env" in keys
    assert not any(k.startswith(ENV_PREFIX) for k in keys)


def test_declared_variables_become_their_own_fields():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert f"{ENV_PREFIX}MIKROTIK_HOST" in fields
    assert f"{ENV_PREFIX}MIKROTIK_PASSWORD" in fields


def test_declared_fields_keep_their_description():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert fields[f"{ENV_PREFIX}MIKROTIK_HOST"].help == "Router address"


def test_a_secret_variable_is_a_password_field():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert fields[f"{ENV_PREFIX}MIKROTIK_PASSWORD"].type == "password"


def test_every_declared_variable_is_encrypted_not_only_the_flagged_ones():
    """Which variables are sensitive is the upstream's claim, and it can be wrong."""
    for field in PLUGIN.fields_for(instance(registry_env=DECLARED)):
        if field.key.startswith(ENV_PREFIX):
            assert field.secret, f"{field.key} would be stored in a plaintext column"


def test_an_ordinary_variable_is_still_shown_back_although_it_is_encrypted():
    """The counterpart to the rule above. Encrypting everything is about a
    stolen database; it is not a reason to hide a router's address from the
    administrator who typed it, and hiding it made a configured backend reopen
    as a blank form."""
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert fields[f"{ENV_PREFIX}MIKROTIK_HOST"].shows_value


def test_a_variable_the_server_calls_secret_is_not_shown_back():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert not fields[f"{ENV_PREFIX}MIKROTIK_PASSWORD"].shows_value


def test_the_freeform_block_is_never_shown_back():
    """Undeclared variables are by definition arbitrary, credentials included."""
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert not fields["env"].shows_value


def test_required_flag_carries_over():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert fields[f"{ENV_PREFIX}MIKROTIK_HOST"].required


def test_freeform_field_remains_for_undeclared_variables():
    fields = {f.key: f for f in PLUGIN.fields_for(instance(registry_env=DECLARED))}
    assert "env" in fields
    assert fields["env"].label == "Additional environment"


# ── env assembly ──────────────────────────────────────────────────────────

def test_env_merges_typed_fields_and_the_freeform_block():
    env = _collect_env(instance(_secrets={f"{ENV_PREFIX}A": "1", "env": "B=2"}))
    assert env == {"A": "1", "B": "2"}


def test_blank_typed_values_are_dropped():
    assert _collect_env(instance(_secrets={f"{ENV_PREFIX}A": ""})) == {}


def test_a_backend_predating_typed_fields_still_works():
    """Older backends stored everything in the freeform block."""
    assert _collect_env(instance(_secrets={"env": "OLD=value"})) == {"OLD": "value"}


# ── the connection selector ───────────────────────────────────────────────

def test_connection_choices_are_labelled():
    field = next(f for f in PLUGIN.fields_for(None) if f.key == CONNECTION_KEY)
    assert dict(choice_pairs(field))["launch"] == "Launch the server here"


def test_switching_to_url_stops_launching_a_stale_command():
    cfg = _upstream(instance(command="uvx old", url="http://new/mcp", connection="url"))._cfg
    assert not cfg.is_stdio
    assert cfg.url == "http://new/mcp"


def test_switching_to_launch_ignores_a_stale_url():
    cfg = _upstream(instance(command="uvx thing", url="http://old/mcp", connection="launch"))._cfg
    assert cfg.is_stdio


@pytest.mark.parametrize("config,expected", [
    ({"command": "uvx thing"}, "launch"),
    ({"url": "http://x/mcp"}, "url"),
])
def test_an_existing_backend_opens_on_the_right_branch(config, expected):
    """Without a stored `connection`, it is inferred from how it is configured."""
    field = next(f for f in PLUGIN.fields_for(instance(**config)) if f.key == CONNECTION_KEY)
    assert field.default == expected


def test_url_only_fields_are_conditional():
    fields = {f.key: f for f in PLUGIN.fields_for(None)}
    assert fields["auth_header"].show_if == (CONNECTION_KEY, "url")
    assert fields["command"].show_if == (CONNECTION_KEY, "launch")


def test_select_fields_are_rendered_by_the_template():
    """A declared type nothing renders is a field that silently becomes a textbox."""
    from pathlib import Path

    template = Path("src/mcphub/web/templates/_fields.html").read_text()
    assert '"select"' in template and "<select" in template


def test_choice_pairs_accepts_bare_strings():
    assert choice_pairs(ConfigField("k", "K", choices=["a"])) == [("a", "a")]
