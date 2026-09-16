"""A saved backend must reopen looking saved.

Adding a backend from the registry writes every variable the server declared
into the encrypted blob — including the ones it did not flag secret, because a
wrong claim should not leave a token in a plaintext column. The settings page
then withheld all of them, so a router whose address, port and password were
all configured reopened as three empty boxes, indistinguishable from a backend
nobody had touched. `secret` had come to mean both "encrypt this" and "never
show this", and those are different questions.
"""

import re

import pytest
from starlette.datastructures import FormData

from mcphub.plugins.base import BackendInstance, ConfigField
from mcphub.plugins.builtin.mcpproxy import ENV_PREFIX, PLUGIN
from mcphub.web.routes import (
    CLEAR_PREFIX,
    TEMPLATES,
    form_values,
    split_fields,
    stored_value,
)

DECLARED = [
    {"name": "THING_HOST", "description": "Router address", "isRequired": True, "isSecret": False},
    {"name": "THING_PORT", "description": "Port", "isRequired": False, "isSecret": False},
    {"name": "THING_TOKEN", "description": "API token", "isRequired": True, "isSecret": True},
]
SAVED_SECRETS = {
    f"{ENV_PREFIX}THING_HOST": "192.168.1.9",
    f"{ENV_PREFIX}THING_PORT": "8728",
    f"{ENV_PREFIX}THING_TOKEN": "s3cret-token",
    "env": "UNDECLARED=1",
}


def configured() -> BackendInstance:
    """A backend as `registry_add` leaves it: everything in the sealed blob."""
    return BackendInstance(
        slug="thing", title="Thing", plugin_id="mcp-proxy",
        config={"registry_env": DECLARED, "connection": "launch",
                "command": "uvx thing", "timeout": 60, "verify_tls": True},
        secrets=dict(SAVED_SECRETS),
    )


async def rendered_fields(instance=None, posted=None):
    return {item["field"].key: item for item in await form_values(PLUGIN, instance, posted=posted)}


# ── storage and display are separate questions ────────────────────────────

def test_secret_alone_still_withholds():
    """The default has to fail safe: a plugin that says nothing keeps today's
    behaviour, rather than having its passwords rendered by an upgrade."""
    assert not ConfigField("k", "K", secret=True).shows_value


def test_a_plain_field_is_shown():
    assert ConfigField("k", "K").shows_value


def test_show_value_overrides_secret_in_both_directions():
    assert ConfigField("k", "K", secret=True, show_value=True).shows_value
    assert not ConfigField("k", "K", show_value=False).shows_value


# ── what the form comes back with ─────────────────────────────────────────

async def test_a_declared_variable_comes_back_filled_in():
    fields = await rendered_fields(configured())
    assert fields[f"{ENV_PREFIX}THING_HOST"]["value"] == "192.168.1.9"


async def test_an_optional_declared_variable_comes_back_too():
    fields = await rendered_fields(configured())
    assert fields[f"{ENV_PREFIX}THING_PORT"]["value"] == "8728"


async def test_a_variable_the_server_flagged_secret_is_still_withheld():
    fields = await rendered_fields(configured())
    assert fields[f"{ENV_PREFIX}THING_TOKEN"]["value"] == ""


async def test_the_freeform_block_is_withheld():
    """It exists for undeclared variables, which is to say arbitrary credentials."""
    assert (await rendered_fields(configured()))["env"]["value"] == ""


async def test_nothing_withheld_ever_carries_its_value():
    """The guarantee lives here, not in the template: a template cannot leak a
    value it was never handed."""
    for key, item in (await rendered_fields(configured())).items():
        if item["withheld"]:
            assert item["value"] == "", f"{key} would be written into the page"


async def test_a_withheld_field_is_marked_as_filled():
    """Otherwise it is an empty box, and an empty box means unconfigured."""
    token = (await rendered_fields(configured()))[f"{ENV_PREFIX}THING_TOKEN"]
    assert token["stored"] and token["withheld"]


async def test_an_unset_secret_is_not_marked_as_filled():
    auth = (await rendered_fields(configured()))["auth_value"]
    assert not auth["stored"] and not auth["withheld"]


async def test_plain_config_still_comes_back():
    fields = await rendered_fields(configured())
    assert fields["command"]["value"] == "uvx thing"
    assert fields["timeout"]["value"] == 60


async def test_a_new_backend_opens_on_its_defaults():
    fields = await rendered_fields(None)
    assert fields["timeout"]["value"] == 30
    assert not fields["command"]["stored"]


# ── a rejected save keeps what was typed ──────────────────────────────────

async def test_a_validation_error_keeps_the_typed_values():
    """A rejected URL name used to revert every other box to what was stored,
    quietly undoing edits that were never the problem."""
    posted = FormData([("command", "uvx thing --new-flag"),
                       (f"{ENV_PREFIX}THING_HOST", "10.0.0.1")])
    fields = await rendered_fields(configured(), posted=posted)
    assert fields["command"]["value"] == "uvx thing --new-flag"
    assert fields[f"{ENV_PREFIX}THING_HOST"]["value"] == "10.0.0.1"


async def test_a_validation_error_does_not_echo_a_withheld_value():
    posted = FormData([(f"{ENV_PREFIX}THING_TOKEN", "just-typed")])
    fields = await rendered_fields(configured(), posted=posted)
    assert fields[f"{ENV_PREFIX}THING_TOKEN"]["value"] == ""


# ── saving ────────────────────────────────────────────────────────────────

BASE_FORM = [("connection", "launch"), ("command", "uvx thing"), ("timeout", "60")]


def save(*extra):
    return split_fields(PLUGIN, FormData(BASE_FORM + list(extra)), configured())


def test_a_blank_withheld_field_keeps_what_is_stored():
    _, secrets, errors = save((f"{ENV_PREFIX}THING_HOST", "192.168.1.9"),
                              (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""))
    assert secrets[f"{ENV_PREFIX}THING_TOKEN"] == "s3cret-token"
    assert secrets["env"] == "UNDECLARED=1"
    assert not errors


def test_a_blank_shown_field_is_a_field_that_was_emptied():
    """It rendered with its value in it, so blank is a deletion, not silence."""
    _, secrets, errors = save((f"{ENV_PREFIX}THING_HOST", ""),
                              (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""))
    assert f"{ENV_PREFIX}THING_HOST" not in secrets
    assert "THING_HOST is required." in errors


def test_an_optional_shown_field_can_be_emptied_without_complaint():
    _, secrets, errors = save((f"{ENV_PREFIX}THING_HOST", "192.168.1.9"),
                              (f"{ENV_PREFIX}THING_PORT", ""),
                              (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""))
    assert f"{ENV_PREFIX}THING_PORT" not in secrets
    assert not errors


def test_a_shown_field_is_updated_in_place():
    _, secrets, _ = save((f"{ENV_PREFIX}THING_HOST", "10.0.0.1"),
                         (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""))
    assert secrets[f"{ENV_PREFIX}THING_HOST"] == "10.0.0.1"


def test_the_clear_box_is_the_way_to_unset_a_withheld_value():
    """Without it there is no way back at all: blank means keep, so an optional
    credential set once could only be removed by deleting the backend."""
    _, secrets, errors = save((f"{ENV_PREFIX}THING_HOST", "192.168.1.9"),
                              (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""),
                              ("clear_env", "on"))
    assert "env" not in secrets
    assert not errors


def test_clearing_a_required_value_is_refused():
    _, _, errors = save((f"{ENV_PREFIX}THING_HOST", "192.168.1.9"),
                        (f"{ENV_PREFIX}THING_TOKEN", ""), ("env", ""),
                        (f"clear_{ENV_PREFIX}THING_TOKEN", "on"))
    assert "THING_TOKEN is required." in errors


def test_a_typed_value_beats_a_ticked_clear_box():
    """Contradictory, but typing is the more specific of the two intentions."""
    _, secrets, _ = save((f"{ENV_PREFIX}THING_HOST", "192.168.1.9"),
                         (f"{ENV_PREFIX}THING_TOKEN", "rotated"), ("env", ""),
                         (f"clear_{ENV_PREFIX}THING_TOKEN", "on"))
    assert secrets[f"{ENV_PREFIX}THING_TOKEN"] == "rotated"


# ── reading a stored value ────────────────────────────────────────────────

def test_stored_value_prefers_the_sealed_blob():
    """A key written to both columns must read back as the encrypted one, which
    is the copy the rest of the hub uses."""
    instance = BackendInstance(slug="s", title="t", plugin_id="p",
                               config={"k": "plain"}, secrets={"k": "sealed"})
    assert stored_value(instance, "k") == "sealed"


@pytest.mark.parametrize("instance,key", [(None, "k"), (configured(), "nothing_here")])
def test_stored_value_is_none_when_nothing_is_saved(instance, key):
    assert stored_value(instance, key) is None


# ── the page itself ───────────────────────────────────────────────────────

async def rendered_page():
    fields = await form_values(PLUGIN, configured())
    return TEMPLATES.get_template("backend_form.html").render(
        plugin=PLUGIN, row={"title": "Thing"}, fields=fields, errors=[], pinnable=False,
        slug="thing", title="Thing", enabled=False, public_url="http://localhost:8080",
        user={"id": 1}, is_admin=True,
    )


async def test_no_withheld_value_reaches_the_page():
    page = await rendered_page()
    assert "s3cret-token" not in page
    assert "UNDECLARED=1" not in page


async def test_a_shown_value_does_reach_the_page():
    assert 'value="192.168.1.9"' in await rendered_page()


async def test_a_filled_but_withheld_box_is_marked_with_dots():
    """The whole complaint was boxes that look empty when they are not."""
    page = await rendered_page()
    token = re.search(r'<input[^>]*name="env_THING_TOKEN"[^>]*>', page)
    assert token and "••••••••" in token.group(0)


async def test_a_filled_but_withheld_box_offers_a_way_to_clear_it():
    """Blank means keep, so without this an optional credential set once could
    only be removed by deleting the backend."""
    assert 'name="clear_env"' in await rendered_page()


async def test_clearing_is_not_offered_where_it_could_only_fail():
    """A required field has no valid empty state; replacing it is the way."""
    assert 'name="clear_env_THING_TOKEN"' not in await rendered_page()


async def test_the_template_and_the_route_agree_on_the_clear_prefix():
    """The template spells the checkbox name out as a literal, so nothing but
    this stops a rename on one side from silently disconnecting the other."""
    assert f'name="{CLEAR_PREFIX}env"' in await rendered_page()


async def test_an_empty_field_keeps_its_own_placeholder():
    """The marker means "something is saved"; it must not displace the example
    text on a field where nothing is."""
    page = await rendered_page()
    auth = re.search(r'<input[^>]*name="auth_value"[^>]*>', page)
    assert auth and 'placeholder="Bearer ..."' in auth.group(0)


async def test_a_withheld_field_is_not_marked_required():
    """Blank keeps the saved value, so the browser must not insist on a retype."""
    page = await rendered_page()
    token = re.search(r'<input[^>]*name="env_THING_TOKEN"[^>]*>', page)
    assert token and "required" not in token.group(0)


async def test_the_strong_password_bubble_stays_off_text_fields():
    """`new-password` on a text input invites the browser to offer to generate
    one, over a field that wants a hostname."""
    page = await rendered_page()
    host = re.search(r'<input[^>]*name="env_THING_HOST"[^>]*>', page)
    assert host and "new-password" not in host.group(0)
    assert host and 'autocomplete="off"' in host.group(0)
