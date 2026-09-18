"""A plugin's own say in whether a configuration may be saved.

The save path could only ever produce two complaints — "is required" and
"must be a number" — and that was the entire vocabulary. A plugin had no way
to refuse a combination of values it knew could not work, so every one of them
saved, mounted, and then quietly did the wrong thing. `on_save` is no help: it
runs after the save is already decided and its exceptions are deliberately
swallowed.

Each proxy case below was confirmed against `_upstream` — the code that
actually assembles the connection — before it was made an error here.
"""

import pytest

from mcphub.plugins.base import (
    BackendInstance,
    ConfigField,
    FieldError,
    PluginDefaults,
    as_field_errors,
)
from mcphub.plugins.builtin.mcpproxy import PLUGIN, _upstream
from mcphub.web.routes import place_errors, run_plugin_validation


def proxy(**config) -> BackendInstance:
    return BackendInstance(slug="s", title="T", plugin_id="mcp-proxy", config=config)


def refusals(**config) -> dict[str, str]:
    """Every problem the proxy finds, keyed by the field it belongs to."""
    return {p.key: p.message for p in PLUGIN.validate(proxy(**config))}


# ── the shape of a refusal ────────────────────────────────────────────────

def test_a_bare_string_is_about_the_whole_form():
    """Two fields that contradict each other belong to neither of them."""
    assert as_field_errors(["these two disagree"]) == [FieldError("these two disagree", "")]


def test_a_field_error_keeps_its_field():
    assert as_field_errors([FieldError("too big", "port")])[0].key == "port"


def test_returning_nothing_means_nothing_is_wrong():
    assert as_field_errors(None) == [] and as_field_errors(()) == []


def test_a_plugin_that_says_nothing_refuses_nothing():
    class Quiet(PluginDefaults):
        id, name, description, fields = "quiet", "Quiet", "d", ()

        def build(self, instance): ...
        async def check(self, instance): ...

    assert run_plugin_validation(Quiet(), proxy()) == []


# ── where a refusal is shown ──────────────────────────────────────────────

def test_a_problem_about_a_field_is_shown_beside_that_field():
    banner, beside = place_errors([FieldError("too big", "port")], {"port"})
    assert banner == [] and beside == {"port": ["too big"]}


def test_a_problem_about_the_form_goes_in_the_banner():
    banner, beside = place_errors([FieldError("these disagree")], {"port"})
    assert banner == ["these disagree"] and beside == {}


def test_a_problem_naming_an_unknown_field_falls_back_to_the_banner():
    """It would otherwise render nowhere at all. Silently losing the reason a
    save was refused is the one outcome worse than an ugly one."""
    banner, beside = place_errors([FieldError("mystery", "no_such_field")], {"port"})
    assert banner == ["mystery"] and beside == {}


def test_several_problems_about_one_field_are_all_kept():
    _, beside = place_errors([FieldError("a", "port"), FieldError("b", "port")], {"port"})
    assert beside == {"port": ["a", "b"]}


# ── a validator that breaks ───────────────────────────────────────────────

def test_a_validator_that_raises_refuses_the_save():
    """It cannot be waved through the way `on_save` is: the point of the hook
    is to stop a configuration, so a broken one has to stop it too. Swallowing
    would save exactly the config the plugin meant to refuse."""
    class Exploding(PluginDefaults):
        id, name, description, fields = "boom", "Boom", "d", ()

        def build(self, instance): ...
        async def check(self, instance): ...
        def validate(self, instance):
            raise RuntimeError("kaboom")

    problems = run_plugin_validation(Exploding(), proxy())
    assert len(problems) == 1
    assert "RuntimeError: kaboom" in problems[0].message
    assert problems[0].key == "", "a broken validator is not about one field"


def test_a_broken_validator_names_the_plugin():
    """Otherwise the message is an unattributable stack type on someone's form."""
    class Exploding(PluginDefaults):
        id, name, description, fields = "culprit", "C", "d", ()

        def build(self, instance): ...
        async def check(self, instance): ...
        def validate(self, instance):
            raise ValueError("nope")

    assert "culprit" in run_plugin_validation(Exploding(), proxy())[0].message


# ── what the proxy now refuses ────────────────────────────────────────────

def test_launching_with_no_command_is_refused():
    assert "command" in refusals(connection="launch", command="")


def test_connecting_with_no_url_is_refused():
    assert "url" in refusals(connection="url", url="")


@pytest.mark.parametrize("url", [
    "192.168.1.50:8043/mcp",   # no scheme
    "ftp://host/mcp",          # not a scheme the client speaks
    "https://",                # no host
    "just some text",
])
def test_a_url_that_is_not_a_url_is_refused(url):
    assert "url" in refusals(connection="url", url=url)


@pytest.mark.parametrize("url", ["http://h:8043/mcp", "https://h/mcp", "https://h"])
def test_a_usable_url_is_accepted(url):
    assert "url" not in refusals(connection="url", url=url)


def test_an_auth_value_with_no_header_name_is_refused():
    """Confirmed against `_upstream`: it sends the header only when it has both
    halves, so this backend authenticates with nothing and does not say so."""
    assert _upstream(proxy(connection="url", url="https://h/mcp",
                           auth_value="Bearer s"))._cfg.headers == {}
    assert "auth_header" in refusals(connection="url", url="https://h/mcp",
                                     auth_value="Bearer s")


def test_a_header_name_with_no_value_is_refused():
    assert "auth_value" in refusals(connection="url", url="https://h/mcp",
                                    auth_header="Authorization")


def test_both_halves_of_the_credential_are_accepted():
    assert not refusals(connection="url", url="https://h/mcp",
                        auth_header="Authorization", auth_value="Bearer s")


def test_no_credential_at_all_is_accepted():
    """Plenty of upstreams need none, which is what the field's help says."""
    assert not refusals(connection="url", url="https://h/mcp")


@pytest.mark.parametrize("timeout", [-5, 0, -0.1])
def test_a_timeout_that_cannot_work_is_refused(timeout):
    assert "timeout" in refusals(connection="launch", command="uvx t", timeout=timeout)


def test_a_blank_timeout_is_not_a_zero_timeout():
    """An empty box became the field default long before reaching here."""
    assert "timeout" not in refusals(connection="launch", command="uvx t", timeout="")


def test_a_typed_zero_is_refused_although_the_runtime_would_tolerate_it():
    """`_upstream` does `timeout or 30`, so a typed 0 silently becomes 30. Being
    stricter than the runtime is the right direction here: the alternative is a
    number you entered being quietly replaced by a different one."""
    assert _upstream(proxy(timeout=0))._cfg.timeout == 30.0
    assert "timeout" in refusals(connection="launch", command="uvx t", timeout=0)


def test_the_auth_pair_is_only_checked_on_the_branch_that_uses_it():
    """Those boxes are hidden when the hub launches the server itself, so
    complaining about them would be a refusal pointing at nothing on screen."""
    assert not refusals(connection="launch", command="uvx t", auth_value="left over")


def test_a_command_is_not_required_on_the_url_branch():
    assert "command" not in refusals(connection="url", url="https://h/mcp")


def test_validation_follows_the_same_rule_upstream_does():
    """`_upstream` infers the branch from the command when none is stored.
    Validating against a different rule than the one that runs would let
    through exactly what this is meant to stop."""
    inferred = proxy(command="uvx thing")
    assert _upstream(inferred)._cfg.is_stdio
    assert not refusals(command="uvx thing")


def test_a_well_formed_launch_backend_is_accepted():
    assert not refusals(connection="launch", command="uvx thing", timeout=60)
