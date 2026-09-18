"""The backend plugin contract.

A plugin describes *a kind of thing you can talk to* (a MikroTik router, an
Unraid server). A **backend instance** is one configured example of that kind.
Each instance is mounted at its own URL and registered in Claude as its own
connector, which is why the tool surfaces of two plugins never merge into one
oversized list.

Plugins are discovered through the ``mcphub.plugins`` entry point group, so a
third-party plugin is just a package you pip install. The built-ins use the
same mechanism with no shortcut.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from mcp.server.mcpserver import MCPServer

FieldType = Literal["text", "password", "number", "bool", "select", "multiselect", "textarea"]

FIELD_TYPES: frozenset[str] = frozenset(get_args(FieldType))
"""Derived from the annotation, so the two can never drift apart."""

WATCHABLE_TYPES: frozenset[str] = frozenset({"select", "bool"})
"""Types a `show_if` may point at.

The settings page re-evaluates conditions on `change` of a `<select>` or a
checkbox and nothing else, so a condition on a text box is read once at page
load and then never again — the dependent field freezes at whatever it was.
"""

CLEAR_PREFIX = "clear_"
"""Namespace for the checkbox that empties a stored value the form withheld.

A field keyed `clear_x` therefore submits under the same name as the clear
instruction for a field keyed `x`, and would delete its stored secret.
"""

RESERVED_FIELD_KEYS: frozenset[str] = frozenset({"plugin_id", "title", "slug", "enabled"})
"""Names the settings form already uses for the backend itself.

A field claiming one of these puts two inputs of the same name in one form,
and the reader takes the first — so the plugin and the hub silently read each
other's value.
"""

FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
"""A key becomes an HTML control name and part of an element id, so it has to
survive both without quoting."""


@dataclass(frozen=True)
class Option:
    """One choice in a `multiselect` field, supplied by the plugin at render time."""

    value: str
    label: str
    help: str = ""


@dataclass(frozen=True)
class ConfigField:
    """One input in the backend's settings form.

    A flat, declarative list rather than a JSON Schema: the config UI needs to
    render a form, not express arbitrary nesting, and keeping it flat means a
    plugin author never has to think about how their schema will look.
    """

    key: str
    label: str
    type: FieldType = "text"
    required: bool = True
    default: Any = None
    help: str = ""
    choices: Sequence[str | tuple[str, str]] = ()
    """Options for a `select`. Either bare values, or (value, label) pairs."""

    show_if: tuple[str, str] | None = None
    """Only show this field when another field has a given value, e.g.
    ``show_if=("connection", "launch")``. Keeps a form from presenting settings
    that cannot apply, which is how a proxy backend ended up showing an auth
    header next to a command line that would ignore it."""

    secret: bool = False
    """Store this value encrypted, in the sealed blob rather than in config_json.

    Storage only. Whether the stored value is rendered back into the form is
    `show_value`, which is a separate question: a variable can be worth
    encrypting without being worth hiding from the person who typed it.
    """

    show_value: bool | None = None
    """Whether the saved value is sent back to the browser when the form reopens.

    `None` follows `secret`, which is the safe default: a plugin that declares
    a field secret and says nothing else keeps it withheld. Set it to True on a
    field that is encrypted at rest but not itself sensitive — a router's
    address stored beside its password — or the settings page reopens showing
    an empty box for a value that is in fact configured, which reads as
    configuration that was lost.
    """

    placeholder: str = ""

    @property
    def shows_value(self) -> bool:
        """Resolve `show_value` against `secret`."""
        return not self.secret if self.show_value is None else self.show_value


def choice_pairs(field: ConfigField) -> list[tuple[str, str]]:
    """Normalise `choices` to (value, label), so templates need not care."""
    pairs: list[tuple[str, str]] = []
    for choice in field.choices:
        if isinstance(choice, tuple):
            pairs.append((str(choice[0]), str(choice[1])))
        else:
            pairs.append((str(choice), str(choice)))
    return pairs


@dataclass(frozen=True)
class BackendInstance:
    """A configured backend, as handed to a plugin at build time."""

    slug: str
    title: str
    plugin_id: str
    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value without caring whether it was stored secret or plain."""
        if key in self.secrets:
            return self.secrets[key]
        return self.config.get(key, default)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str
    """One line, shown next to the Test button. On failure, say what to fix."""


@runtime_checkable
class Plugin(Protocol):
    """What a backend must provide.

    Implemented as a Protocol rather than a base class so a plugin does not
    have to import and subclass anything from the hub to be usable.
    """

    id: str
    name: str
    description: str
    fields: Sequence[ConfigField]

    def build(self, instance: BackendInstance) -> MCPServer:
        """Return the MCP server for this instance.

        Called once at mount time and whenever the instance's config changes.
        Must not perform network I/O: a backend that is merely unreachable has
        to still mount, so its tools can report the failure themselves rather
        than the whole hub refusing to start.
        """
        ...

    async def check(self, instance: BackendInstance) -> CheckResult:
        """Verify credentials and reachability for the settings UI."""
        ...

    # ── optional ──────────────────────────────────────────────────────────
    # Both have defaults in `PluginDefaults`; a plugin that does not need them
    # simply omits them.

    async def options(self, instance: BackendInstance, key: str) -> Sequence[Option]:
        """Choices for a `multiselect` field, fetched when the form is rendered.

        Network I/O is fine here — unlike `build`, this runs inside a request
        and may fail without taking the backend down.
        """
        ...

    async def on_save(self, instance: BackendInstance) -> dict[str, Any]:
        """Extra config to persist after a successful save.

        Lets a plugin cache something it discovered — an upstream tool catalogue,
        say — so that `build` can stay offline.
        """
        ...

    def variant(self, instance: BackendInstance, version: str) -> BackendInstance:
        """The same backend as it should run at a particular version.

        Returning `instance` unchanged means the plugin does not do versions,
        and every account gets the one configuration.
        """
        ...

    def fields_for(self, instance: BackendInstance | None) -> Sequence[ConfigField]:
        """The settings form for one backend, which need not be the generic one.

        A backend added from the registry knows which variables its server
        declares, so its form can name them with their own descriptions instead
        of falling back to a freeform blob and an example about some other
        server's API key.
        """
        ...

    def tool_names(self, instance: BackendInstance) -> set[str]:
        """What this backend currently believes it exposes.

        Read before and after an Update so the result can say what changed
        rather than only that something did. Returning an empty set means the
        plugin does not track this, and Update reports no difference.
        """
        ...

    review_before_enable: bool
    """Create new backends of this kind disabled.

    Set it where the tool surface comes from somewhere other than this
    repository. A newly added third-party server should not be able to attach
    its tools to an account before anyone has looked at what they are.
    """


class PluginDefaults:
    """Mix in to inherit no-op implementations of the optional hooks."""

    fields: Sequence[ConfigField] = ()

    def fields_for(self, instance: BackendInstance | None) -> Sequence[ConfigField]:
        return self.fields

    def variant(self, instance: BackendInstance, version: str) -> BackendInstance:
        return instance

    review_before_enable: bool = False
    """Create new backends of this kind disabled.

    Set it where the tool surface comes from somewhere other than this
    repository. A newly added third-party server should not be able to attach
    its tools to an account before anyone has looked at what they are.
    """

    async def options(self, instance: BackendInstance, key: str) -> Sequence[Option]:
        return ()

    async def on_save(self, instance: BackendInstance) -> dict[str, Any]:
        return {}

    def tool_names(self, instance: BackendInstance) -> set[str]:
        return set()


REQUIRED_ATTRIBUTES = ("id", "name", "description", "fields", "build", "check")
"""What a plugin must supply itself. There is no default for any of these."""

OPTIONAL_ATTRIBUTES = ("fields_for", "options", "on_save", "variant", "tool_names",
                       "review_before_enable")
"""Hooks the hub calls unconditionally, and `PluginDefaults` answers for free.

They are optional to *write*, not optional to *have*: mix in `PluginDefaults`
and every one is supplied. A plugin that mixes in neither and implements none
of them would raise at render time instead, in a request, with the settings
page half drawn.
"""


def field_problems(fields: Sequence[ConfigField]) -> list[str]:
    """Everything wrong with one plugin's declared form, in one pass.

    Collected rather than raised one at a time so a plugin author fixes the
    whole form in a single edit, instead of rediscovering the next defect on
    each restart.
    """
    problems: list[str] = []
    keys = [f.key for f in fields if isinstance(f, ConfigField)]
    by_key = {f.key: f for f in fields if isinstance(f, ConfigField)}
    seen: set[str] = set()

    for f in fields:
        if not isinstance(f, ConfigField):
            problems.append(f"fields must be ConfigField, got {type(f).__name__}")
            continue

        # ── the key ───────────────────────────────────────────────────────
        if not isinstance(f.key, str):
            # Everything below indexes, matches or concatenates it.
            problems.append(f"a field key must be a string, got {type(f.key).__name__}")
            continue
        if not FIELD_KEY_RE.match(f.key):
            problems.append(
                f"{f.key!r} is not usable as a form control name; it must start with a "
                "letter or underscore and contain only letters, digits, dot, dash or "
                "underscore"
            )
        if f.key in seen:
            problems.append(f"duplicate config field {f.key!r}")
        seen.add(f.key)
        if f.key in RESERVED_FIELD_KEYS:
            problems.append(
                f"{f.key!r} is reserved: the settings form already uses that name for the "
                "backend itself, so the two would read each other's value"
            )
        if f.key.startswith(CLEAR_PREFIX) and f.key[len(CLEAR_PREFIX):] in keys:
            shadowed = f.key[len(CLEAR_PREFIX):]
            problems.append(
                f"{f.key!r} collides with the checkbox that clears {shadowed!r}; a value "
                f"typed here would delete the stored {shadowed!r} instead"
            )

        # ── the type ──────────────────────────────────────────────────────
        if f.type not in FIELD_TYPES:
            problems.append(
                f"{f.key!r} has unknown type {f.type!r}; the form would silently render it "
                f"as a text box. Known types: {', '.join(sorted(FIELD_TYPES))}"
            )

        # ── the default, against the type ─────────────────────────────────
        if f.type == "select":
            pairs = choice_pairs(f)
            if not pairs:
                problems.append(f"{f.key!r} is a select with no choices, so it renders empty")
            elif f.default is not None and str(f.default) not in {v for v, _ in pairs}:
                problems.append(
                    f"{f.key!r} defaults to {f.default!r}, which is not one of its choices "
                    f"({', '.join(v for v, _ in pairs)}); the browser would select the first "
                    "instead and the declared default would never apply"
                )
        if f.type == "number" and f.default is not None and (
            # bool is a subclass of int, so it has to be excluded explicitly.
            not isinstance(f.default, (int, float)) or isinstance(f.default, bool)
        ):
            problems.append(f"{f.key!r} is a number field with a non-numeric default {f.default!r}")
        if f.type == "bool" and f.default is not None and not isinstance(f.default, bool):
            problems.append(f"{f.key!r} is a checkbox with a non-boolean default {f.default!r}")

        # ── the condition ─────────────────────────────────────────────────
        if f.show_if is None:
            continue
        other = f.show_if[0]
        if other == f.key:
            problems.append(f"{f.key!r} is conditional on itself")
        elif other not in by_key:
            problems.append(
                f"{f.key!r} is conditional on {other!r}, which this plugin does not declare; "
                "the form cannot find the control and leaves the field permanently visible"
            )
        elif by_key[other].type not in WATCHABLE_TYPES:
            problems.append(
                f"{f.key!r} is conditional on {other!r}, a {by_key[other].type!r} field. Only "
                f"{' and '.join(sorted(WATCHABLE_TYPES))} fields are watched for changes, so "
                "the condition would be read once at page load and never again"
            )
        elif by_key[other].show_if is not None:
            problems.append(
                f"{f.key!r} is conditional on {other!r}, which is itself conditional. Each "
                "condition is evaluated on its own, so this field would show whenever "
                f"{other!r} holds the right value even while {other!r} is hidden"
            )

    return problems


def validate_plugin(obj: object) -> Plugin:
    """Fail loudly at load time rather than at first request.

    Everything checked here is otherwise silent: a select with no choices, a
    default that never applies, a condition that never fires. None of them
    raise — they just make the settings page quietly wrong, which is a long
    way to walk back from a form that looks fine.
    """
    missing = [attr for attr in REQUIRED_ATTRIBUTES if not hasattr(obj, attr)]
    if missing:
        raise TypeError(f"{obj!r} is not a valid mcphub plugin; missing: {', '.join(missing)}")

    name = getattr(obj, "id", obj)
    unanswered = [attr for attr in OPTIONAL_ATTRIBUTES if not hasattr(obj, attr)]
    if unanswered:
        raise TypeError(
            f"plugin {name!r} does not answer: {', '.join(unanswered)}. Mix in "
            "`mcphub.plugins.base.PluginDefaults` to inherit them, or implement them."
        )

    problems = field_problems(list(obj.fields))  # type: ignore[attr-defined]
    if problems:
        raise ValueError(
            f"plugin {name!r} has {len(problems)} problem(s) in its settings form:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return obj  # type: ignore[return-value]
