# Writing a plugin

A plugin is a Python package that describes a *kind* of backend: what it needs
to be configured with, how to test that configuration, and how to build the MCP
server for one instance of it. The built-in proxy plugin is one of these; so is
the MikroTik one.

You do not need a plugin to be used through mcphub. If your server speaks MCP
over stdio or HTTP, the proxy plugin already wraps it and you can stop here.
Write a plugin when the generic "URL and headers" form is the wrong form —
when your backend has real settings that deserve real fields, dropdowns filled
from the device, and a Test button that says something useful.

## The shape

```python
from mcphub.plugins.base import (
    BackendInstance, CheckResult, ConfigField, FieldError, PluginDefaults,
)
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations


class DictionaryPlugin(PluginDefaults):
    id = "dictionary"
    name = "Aenvae dictionary"
    description = "Look words up and propose new ones."

    fields = (
        ConfigField("host", "Host", placeholder="10.0.0.20"),
        ConfigField("token", "API token", type="password", secret=True),
        ConfigField("readonly", "Read-only", type="bool", default=False),
    )

    def build(self, instance: BackendInstance) -> MCPServer:
        server = MCPServer(instance.title)

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def lookup(word: str) -> str:
            """Definition of one word."""
            return client(instance).lookup(word)

        return server

    async def check(self, instance: BackendInstance) -> CheckResult:
        try:
            info = await client(instance).ping()
        except Exception as exc:
            return CheckResult(False, f"Could not reach it: {exc}")
        return CheckResult(True, f"Connected to {info.name}.")


PLUGIN = DictionaryPlugin()
```

Register it on the entry point group so the hub finds it:

```toml
[project.entry-points."mcphub.plugins"]
dictionary = "mcphub_dictionary:PLUGIN"
```

Install it into the same environment as the hub. A plugin that fails to import
is logged and skipped rather than taking the hub down with it.

## What `PluginDefaults` gives you

Sensible implementations of everything except `build` and `check`, so a small
plugin stays small. Override these when you need them:

| Method | |
| --- | --- |
| `fields_for(instance)` | A form that depends on the backend, rather than the fixed `fields` |
| `options(instance, key)` | Fill a `select` or `multiselect` from the device itself |
| `validate(instance)` | Refuse a configuration, returning `FieldError`s |
| `variant(instance, version)` | The same backend at a pinned version |
| `on_save` / `on_delete` | Provision and release whatever the backend needs elsewhere |
| `review_before_enable` | Create new backends disabled, for tool surfaces from elsewhere |

Two rules worth stating:

- **Mark secrets `secret=True`.** They are sealed with the hub's key and never
  redisplayed; the form shows that something is stored without showing what.
- **Build a variant with `dataclasses.replace`.** Listing `BackendInstance`
  fields by hand drops any added later — silently, and only at a pinned
  version, which is the hardest kind of difference to notice.

## Refusing a configuration

Return `FieldError`s from `validate`, naming the field each is about, so the
message lands under the box rather than in a banner at the top of a long form.

```python
def validate(self, instance):
    if instance.get("readonly") and instance.get("write_token"):
        return [FieldError("write_token", "Not used while read-only is on.")]
    return []
```

Leave `key` empty for something true of the form as a whole — two fields that
contradict each other belong to neither.

## Storage

`instance.storage` is your directory, or `None` if the hub could not create one
(a read-only volume, say). If your plugin needs storage, say so in `check`
rather than writing into nowhere. See `storage`.
