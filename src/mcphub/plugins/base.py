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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from mcp.server.mcpserver import MCPServer

FieldType = Literal["text", "password", "number", "bool", "select", "multiselect", "textarea"]


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


def validate_plugin(obj: object) -> Plugin:
    """Fail loudly at load time rather than at first request."""
    missing = [
        attr for attr in ("id", "name", "description", "fields", "build", "check")
        if not hasattr(obj, attr)
    ]
    if missing:
        raise TypeError(f"{obj!r} is not a valid mcphub plugin; missing: {', '.join(missing)}")

    seen: set[str] = set()
    for f in obj.fields:  # type: ignore[attr-defined]
        if not isinstance(f, ConfigField):
            raise TypeError(f"plugin {obj.id!r}: fields must be ConfigField, got {type(f).__name__}")  # type: ignore[attr-defined]
        if f.key in seen:
            raise ValueError(f"plugin {obj.id!r}: duplicate config field {f.key!r}")  # type: ignore[attr-defined]
        seen.add(f.key)
    return obj  # type: ignore[return-value]
