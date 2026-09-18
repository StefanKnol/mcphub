"""What the plugin contract refuses to accept.

`validate_plugin` exists to "fail loudly at load time rather than at first
request", and for a long time it did almost none of that: it checked that six
attributes existed and that no two fields shared a key, and waved through a
select with no choices, a default that could never apply, a condition that
could never fire, and a field key that silently deleted another field's stored
secret. None of those raise on their own. They just make the settings page
quietly wrong, which is a long way to walk back from a form that looks fine.

Every case below was confirmed against the running form before it was made an
error here.
"""

import logging
import re
from pathlib import Path

import pytest

from mcphub.plugins.base import (
    CLEAR_PREFIX,
    FIELD_TYPES,
    OPTIONAL_ATTRIBUTES,
    RESERVED_FIELD_KEYS,
    BackendInstance,
    ConfigField,
    PluginDefaults,
    field_problems,
    show_if_values,
    validate_plugin,
)
from mcphub.plugins.builtin.mcpproxy import PLUGIN
from mcphub.plugins.registry import PluginRegistry


def plugin_with(*fields: ConfigField):
    class Candidate(PluginDefaults):
        id, name, description = "candidate", "Candidate", "A plugin under test."

        def build(self, instance):  # pragma: no cover - never reached
            raise AssertionError

        async def check(self, instance):  # pragma: no cover - never reached
            raise AssertionError

    Candidate.fields = fields
    return Candidate()


def problems(*fields: ConfigField) -> str:
    return "\n".join(field_problems(list(fields)))


# ── the built-in has to keep passing ──────────────────────────────────────

def test_the_shipped_proxy_plugin_validates():
    """The rules are only worth having if the one real plugin obeys them."""
    assert validate_plugin(PLUGIN) is PLUGIN


def test_a_plain_well_formed_plugin_validates():
    good = plugin_with(
        ConfigField("mode", "Mode", type="select", choices=(("a", "A"), ("b", "B")), default="a"),
        ConfigField("host", "Host", show_if=("mode", "a")),
        ConfigField("port", "Port", type="number", default=443),
        ConfigField("tls", "TLS", type="bool", default=True),
    )
    assert validate_plugin(good) is good


# ── keys ──────────────────────────────────────────────────────────────────

def test_a_duplicate_key_is_refused():
    assert "duplicate config field 'k'" in problems(
        ConfigField("k", "One"), ConfigField("k", "Two"))


@pytest.mark.parametrize("key", sorted(RESERVED_FIELD_KEYS))
def test_a_key_the_form_already_uses_is_refused(key):
    """Two inputs of one name in one form, and the reader takes the first — so
    the plugin and the hub read each other's value."""
    assert "reserved" in problems(ConfigField(key, "Theirs"))


def test_a_key_that_shadows_a_clear_checkbox_is_refused():
    """Confirmed against the real save path: typing into a field keyed
    `clear_token` deleted the stored `token` secret outright."""
    found = problems(ConfigField("token", "Token", secret=True),
                     ConfigField(f"{CLEAR_PREFIX}token", "Clear token"))
    assert "collides with the checkbox that clears 'token'" in found


def test_the_clear_prefix_alone_is_not_forbidden():
    """Only the collision matters. `clear_cache` is a perfectly good setting
    when nothing is keyed `cache`."""
    assert not problems(ConfigField(f"{CLEAR_PREFIX}cache", "Clear the cache"))


@pytest.mark.parametrize("key", ["", "has space", "quote\"d", "1leading-digit", "br{ace}"])
def test_a_key_that_is_not_a_usable_control_name_is_refused(key):
    assert "not usable as a form control name" in problems(ConfigField(key, "Bad"))


# ── types and defaults ────────────────────────────────────────────────────

def test_an_unknown_type_is_refused():
    """The template's else-branch renders anything it does not recognise as a
    text box, so a typo in `type` becomes a silently wrong control."""
    assert "unknown type" in problems(ConfigField("k", "K", type="colour-picker"))


@pytest.mark.parametrize("kind", sorted(FIELD_TYPES))
def test_every_declared_type_is_accepted(kind):
    """The known-type set is derived from the annotation; this proves it did
    not drift from what the form can actually render."""
    field = (ConfigField("k", "K", type=kind, choices=("a",))
             if kind == "select" else ConfigField("k", "K", type=kind))
    assert not problems(field)


def test_a_select_with_no_choices_is_refused():
    assert "select with no choices" in problems(ConfigField("k", "K", type="select"))


def test_a_select_default_outside_its_choices_is_refused():
    """The browser selects the first option instead, so the declared default
    silently never applies."""
    found = problems(ConfigField("k", "K", type="select", choices=("a", "b"), default="z"))
    assert "not one of its choices" in found


def test_a_select_may_get_its_choices_from_the_plugin_instead():
    """Some choices are only knowable at render time — the interfaces a router
    actually has — which a literal list cannot express."""
    assert not problems(ConfigField("k", "K", type="select", choices_from_plugin=True))


def test_a_select_cannot_both_list_choices_and_ask_for_them():
    """Only the plugin's would be shown, so the list is a lie about the form."""
    found = problems(ConfigField("k", "K", type="select", choices=("a",), choices_from_plugin=True))
    assert "both lists choices and asks the plugin" in found


@pytest.mark.parametrize("kind", ["text", "number", "bool", "textarea"])
def test_asking_for_choices_where_there_are_none_to_show_is_refused(kind):
    found = problems(ConfigField("k", "K", type=kind, choices_from_plugin=True))
    assert "which only a select or a multiselect has" in found


def test_a_multiselect_always_asks_the_plugin():
    """It has no static list and never had one."""
    assert ConfigField("k", "K", type="multiselect").asks_the_plugin_for_choices


def test_a_plain_select_does_not():
    assert not ConfigField("k", "K", type="select", choices=("a",)).asks_the_plugin_for_choices


def test_a_condition_on_a_dynamic_select_is_not_second_guessed():
    """Its choices are not known until the form is drawn, so nothing here can
    say the value will never be among them."""
    assert not problems(
        ConfigField("mode", "Mode", type="select", choices_from_plugin=True),
        ConfigField("k", "K", show_if=("mode", "whatever-the-server-says")))


def test_a_select_default_may_be_omitted():
    assert not problems(ConfigField("k", "K", type="select", choices=("a", "b")))


def test_a_non_numeric_default_on_a_number_field_is_refused():
    assert "non-numeric default" in problems(
        ConfigField("k", "K", type="number", default="not-a-number"))


@pytest.mark.parametrize("default", [0, 30, 1.5])
def test_a_numeric_default_is_accepted(default):
    assert not problems(ConfigField("k", "K", type="number", default=default))


@pytest.mark.parametrize("default", [True, [1], {"a": 1}, object()])
def test_anything_that_is_not_a_number_is_refused_as_a_number_default(default):
    """`True` is the one that slips through a naive check: bool subclasses int."""
    assert "non-numeric default" in problems(
        ConfigField("k", "K", type="number", default=default))


def test_a_non_string_key_is_reported_rather_than_crashing():
    """Everything the key checks do — match, index, concatenate — assumes a
    string, so an int key took the validator down instead of failing it."""
    assert "must be a string" in problems(ConfigField(42, "Bad"))


def test_a_non_boolean_default_on_a_checkbox_is_refused():
    assert "non-boolean default" in problems(ConfigField("k", "K", type="bool", default="yes"))


def test_false_is_a_valid_checkbox_default():
    """It is falsy, which is exactly how a naive check would lose it."""
    assert not problems(ConfigField("k", "K", type="bool", default=False))


# ── conditions ────────────────────────────────────────────────────────────

def test_a_condition_on_an_undeclared_field_is_refused():
    """base.html gives up when it cannot find the control, leaving the field
    permanently visible — the opposite of what was asked for."""
    assert "does not declare" in problems(ConfigField("k", "K", show_if=("ghost", "yes")))


def test_a_condition_on_a_text_field_is_accepted():
    """The page listens for `input` as well as `change`, so a condition on a
    text box keeps up with typing instead of freezing at page load."""
    assert not problems(ConfigField("driver", "Driver"),
                        ConfigField("k", "K", show_if=("driver", "x")))


def test_a_condition_on_a_checkbox_is_accepted():
    assert not problems(ConfigField("on", "On", type="bool"),
                        ConfigField("k", "K", show_if=("on", "true")))


@pytest.mark.parametrize("spelling", ["yes", "on", "1", "True"])
def test_a_checkbox_condition_spelled_any_other_way_is_refused(spelling):
    """A checkbox has no value attribute, so its `.value` reads "on" ticked or
    not. Comparing against that is how a condition on one meant a field that
    could never appear."""
    found = problems(ConfigField("on", "On", type="bool"),
                     ConfigField("k", "K", show_if=("on", spelling)))
    assert "can never match" in found


def test_a_chain_of_conditions_is_accepted():
    """A branch may have sub-branches: the page resolves a field's controller
    before the field, so one whose controller is hidden is hidden too."""
    assert not problems(
        ConfigField("mode", "Mode", type="select", choices=("a", "b")),
        ConfigField("sub", "Sub", type="select", choices=("x", "y"), show_if=("mode", "a")),
        ConfigField("leaf", "Leaf", show_if=("sub", "x")),
    )


def test_a_loop_of_conditions_is_refused():
    """Chaining resolves; a loop has no starting point to resolve from."""
    found = problems(ConfigField("a", "A", show_if=("b", "1")),
                     ConfigField("b", "B", show_if=("a", "1")))
    assert "form a loop" in found


def test_a_longer_loop_is_refused_too():
    found = problems(ConfigField("a", "A", show_if=("b", "1")),
                     ConfigField("b", "B", show_if=("c", "1")),
                     ConfigField("c", "C", show_if=("a", "1")))
    assert "form a loop" in found


def test_one_loop_is_reported_once():
    """Every field in it would otherwise report the same loop."""
    found = field_problems([ConfigField("a", "A", show_if=("b", "1")),
                            ConfigField("b", "B", show_if=("a", "1"))])
    assert len(found) == 1


def test_several_accepted_values_are_allowed():
    assert not problems(ConfigField("mode", "Mode", type="select", choices=("a", "b", "c")),
                        ConfigField("k", "K", show_if=("mode", ("a", "c"))))


def test_waiting_for_a_value_the_select_never_offers_is_refused():
    """The field could never appear, and nothing would ever say why."""
    found = problems(ConfigField("mode", "Mode", type="select", choices=("a", "b")),
                     ConfigField("k", "K", show_if=("mode", "z")))
    assert "can never appear" in found


def test_a_condition_naming_no_value_at_all_is_refused():
    assert "names no value to match" in problems(
        ConfigField("mode", "Mode", type="select", choices=("a",)),
        ConfigField("k", "K", show_if=("mode", ())))


@pytest.mark.parametrize("kind", ["multiselect", "textarea"])
def test_a_condition_on_a_field_with_no_single_value_is_refused(kind):
    found = problems(ConfigField("many", "Many", type=kind),
                     ConfigField("k", "K", show_if=("many", "x")))
    assert "no single value to compare against" in found


def test_show_if_values_normalises_both_spellings():
    assert show_if_values(ConfigField("k", "K", show_if=("m", "a"))) == ["a"]
    assert show_if_values(ConfigField("k", "K", show_if=("m", ("a", "b")))) == ["a", "b"]
    assert show_if_values(ConfigField("k", "K")) == []


def test_a_self_referential_condition_is_refused():
    assert "conditional on itself" in problems(
        ConfigField("k", "K", type="select", choices=("a",), show_if=("k", "a")))


# ── the hooks the hub calls unconditionally ───────────────────────────────

@pytest.mark.parametrize("hook", OPTIONAL_ATTRIBUTES)
def test_plugin_defaults_answers_every_optional_hook(hook):
    """`PluginDefaults` is the offered way to satisfy these, so it had better
    satisfy all of them."""
    assert hasattr(plugin_with(), hook)


def test_a_plugin_answering_none_of_the_hooks_is_refused():
    """They are optional to write, not optional to have. Without them the
    failure lands at render time, in a request, with the page half drawn."""
    class Bare:
        id, name, description, fields = "bare", "Bare", "d", ()

        def build(self, instance): ...
        async def check(self, instance): ...

    with pytest.raises(TypeError, match="PluginDefaults"):
        validate_plugin(Bare())


def test_tool_names_is_part_of_the_contract_now():
    """It was called through getattr with a silent fallback, so a plugin that
    did not happen to implement it reported 'nothing changed' on every Update
    and had no way to find out why."""
    assert plugin_with().tool_names(
        BackendInstance(slug="s", title="t", plugin_id="candidate")) == set()


def test_review_before_enable_is_part_of_the_contract_now():
    assert plugin_with().review_before_enable is False


def test_a_missing_required_attribute_still_names_what_is_missing():
    class Nameless(PluginDefaults):
        id, description, fields = "x", "d", ()

        def build(self, instance): ...
        async def check(self, instance): ...

    with pytest.raises(TypeError, match="name"):
        validate_plugin(Nameless())


# ── how the failure surfaces ──────────────────────────────────────────────

def test_every_problem_is_reported_at_once():
    """One restart per defect is a miserable way to write a plugin."""
    found = field_problems([
        ConfigField("mode", "Mode", type="select"),
        ConfigField("port", "Port", type="number", default="nope"),
        ConfigField("slug", "Slug"),
    ])
    assert len(found) == 3


def test_the_error_counts_the_problems_and_lists_them():
    with pytest.raises(ValueError) as caught:
        validate_plugin(plugin_with(ConfigField("mode", "Mode", type="select"),
                                    ConfigField("slug", "Slug")))
    message = str(caught.value)
    assert "2 problem(s)" in message
    assert message.count("\n  - ") == 2


def test_the_readme_example_would_actually_load():
    """It did not. The documented plugin mixed in nothing, so the rule that a
    plugin must answer every hook would have refused the one example anybody
    copies from."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    example = re.search(r"^class (\w+)\(([^)]*)\):", readme, re.MULTILINE)
    assert example, "the plugin example vanished from the README"
    assert "PluginDefaults" in example.group(2), (
        f"README example `class {example.group(1)}({example.group(2)})` would be refused "
        "at load: it answers none of the optional hooks."
    )


def test_the_readme_names_every_reserved_key():
    """The list is only useful to a plugin author if it is the real list."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    for key in RESERVED_FIELD_KEYS:
        assert f"`{key}`" in readme, f"{key!r} is reserved but the README never says so"


def test_a_broken_plugin_is_skipped_rather_than_taking_the_hub_down(monkeypatch, caplog):
    """One bad third-party backend must not stop you reaching the others —
    which is precisely when you need the settings page."""
    class Entry:
        name = "broken"

        def load(self):
            return plugin_with(ConfigField("mode", "Mode", type="select"))

    monkeypatch.setattr("mcphub.plugins.registry.entry_points", lambda group: [Entry()])
    registry = PluginRegistry()
    with caplog.at_level(logging.ERROR):
        registry.load_entry_points()

    assert len(registry) == 0
    assert "failed to load and was skipped" in caplog.text
    assert "select with no choices" in caplog.text
