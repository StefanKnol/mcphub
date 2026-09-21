"""Re-expose an existing MCP server through the hub.

The point is not the pipe, it is what wrapping buys you:

- **Authentication for a server that has none.** Point this at a server on the
  LAN and it inherits the hub's OAuth, dynamic client registration and
  per-backend token scoping without a line of its own code changing.
- **A tool allowlist.** A big upstream (the Unraid agent is 126 tools, roughly
  10,000 tokens just to list) can be narrowed to the handful you actually use.
- **Several views of one server.** Point two backends at the same upstream with
  different selections — a read-only one and an admin one — and each is its own
  connector with its own token.

Once wrapped, firewall the upstream's own port to the hub. Otherwise the
authentication is decorative: the original open port is still there.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.types import Prompt as UpstreamPrompt
from mcp.types import Resource as UpstreamResource
from mcp.types import Tool as UpstreamTool

from ...base import (
    BackendInstance,
    CheckResult,
    ConfigField,
    FieldError,
    Option,
    PluginDefaults,
)
from .... import appaccess
from .... import storage
from .mirror import mirror_prompt, mirror_resource, mirror_tool
from .upstream import Upstream, UpstreamConfig, UpstreamError

log = logging.getLogger(__name__)

CATALOG_KEY = "tool_catalog"
RESOURCE_CATALOG_KEY = "resource_catalog"
PROMPT_CATALOG_KEY = "prompt_catalog"
ALLOW_KEY = "tools"
REGISTRY_ENV_KEY = "registry_env"
"""Cached declaration of the variables an upstream asks for, from its registry
entry. Kept so the settings form can name them offline, rather than degrading
to a freeform blob once the backend exists."""

ENV_PREFIX = "env_"
"""Declared variables are stored one per key, so each can be its own field."""

CONNECTION_KEY = "connection"
VERSION_KEY = "upstream_version"
NAME_KEY = "upstream_name"
PACKAGE_KEY = "registry_package"
VERSION_CATALOGS_KEY = "version_catalogs"
"""Per-version catalogues, keyed by version string. A pinned version can offer
a different set of tools, and offering an account a tool its own version does
not have would fail only when it tried to use it."""


def _parse_env(raw: str) -> dict[str, str]:
    """KEY=VALUE per line. Blank lines and # comments ignored."""
    env: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _collect_env(instance: BackendInstance) -> dict[str, str]:
    """Every environment variable for the launched server.

    Two sources, because both exist: variables the upstream declared are stored
    one per key so the form can type them, and the freeform textarea covers
    anything it did not declare. Older backends only have the textarea.

    Whichever it came from, `$MCPHUB_STORAGE` in a value becomes the backend's
    own directory. Almost no server asks for its data path under that name — it
    wants `DB_PATH` or `--state-dir` or whatever it chose — so being able to
    write `DB_PATH=$MCPHUB_STORAGE/app.db` is what makes the directory usable
    at all. Nothing else would expand it: a launched server is handed its
    environment directly, with no shell in between.
    """
    env = _parse_env(str(instance.get("env", "") or ""))
    for source in (instance.config, instance.secrets):
        for key, value in source.items():
            if key.startswith(ENV_PREFIX) and value not in (None, ""):
                env[key[len(ENV_PREFIX):]] = str(value)
    if instance.storage is not None:
        env = {storage.ENV_VAR: str(instance.storage),
               **{k: storage.expand(v, instance.storage) for k, v in env.items()}}
    # Last, and not expandable: these are credentials the hub minted, not
    # settings, and a configured value must not be able to shadow one.
    return {**env, **appaccess.environment_for(instance)}


def _declared_env(instance: BackendInstance) -> list[dict[str, Any]]:
    raw = instance.config.get(REGISTRY_ENV_KEY)
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, dict) and v.get("name")]
    if isinstance(raw, str) and raw:
        try:
            return [v for v in json.loads(raw) if v.get("name")]
        except (json.JSONDecodeError, AttributeError, TypeError):
            return []
    return []


def _upstream(instance: BackendInstance) -> Upstream:
    headers: dict[str, str] = {}
    header_name = str(instance.get("auth_header", "") or "").strip()
    header_value = str(instance.get("auth_value", "") or "").strip()
    if header_name and header_value:
        headers[header_name] = header_value
    # An app in another container cannot be handed an environment, so the only
    # channel to it is the connection the hub already opens.
    headers.update(appaccess.header_for(instance))
    command = str(instance.get("command", "") or "").strip()
    url = str(instance.get("url", "") or "").strip()
    # Whichever the Connection field says wins. Without this, switching a
    # backend from a launched server to a URL would silently keep launching,
    # because a non-empty command always selects stdio.
    connection = str(instance.get(CONNECTION_KEY, "") or "") or ("launch" if command else "url")
    if connection == "url":
        command = ""
    else:
        url = ""

    return Upstream(UpstreamConfig(
        url=url,
        command=command,
        env=_collect_env(instance),
        headers=headers,
        timeout=float(instance.get("timeout", 30) or 30),
        verify_tls=bool(instance.get("verify_tls", True)),
    ))


def _catalog(instance: BackendInstance) -> list[UpstreamTool]:
    """Tool schemas cached at save time, so `build` needs no network."""
    raw = instance.config.get(CATALOG_KEY) or "[]"
    try:
        return [UpstreamTool.model_validate(t) for t in json.loads(raw)]
    except Exception:  # noqa: BLE001 - a stale cache must not break mounting
        log.warning("backend %s: tool catalogue could not be read", instance.slug)
        return []


def _resource_catalog(instance: BackendInstance) -> list[UpstreamResource]:
    raw = instance.config.get(RESOURCE_CATALOG_KEY) or "[]"
    try:
        return [UpstreamResource.model_validate(r) for r in json.loads(raw)]
    except Exception:  # noqa: BLE001 - a stale cache must not break mounting
        log.warning("backend %s: resource catalogue could not be read", instance.slug)
        return []


def _prompt_catalog(instance: BackendInstance) -> list[UpstreamPrompt]:
    raw = instance.config.get(PROMPT_CATALOG_KEY) or "[]"
    try:
        return [UpstreamPrompt.model_validate(r) for r in json.loads(raw)]
    except Exception:  # noqa: BLE001 - a stale cache must not break mounting
        log.warning("backend %s: prompt catalogue could not be read", instance.slug)
        return []


def _allowed(instance: BackendInstance) -> set[str] | None:
    """The selected tool names, or None meaning "expose everything"."""
    raw = instance.config.get(ALLOW_KEY)
    if not raw:
        return None
    names = {n.strip() for n in (raw if isinstance(raw, list) else str(raw).split(",")) if n.strip()}
    return names or None


class McpProxyPlugin(PluginDefaults):
    id = "mcp-proxy"
    name = "MCP server (proxy)"
    description = (
        "Put an existing MCP server behind this hub's authentication, and choose "
        "which of its tools to expose."
    )

    review_before_enable = True

    # The generic form, for a backend added by hand. A backend that came from
    # the registry gets a form shaped by what its server declared — see
    # `fields_for`, which is what the settings page actually renders.
    fields = (
        ConfigField(
            CONNECTION_KEY, "Connection", type="select", required=False, default="launch",
            choices=(("launch", "Launch the server here"), ("url", "Connect to a running server")),
            help="Whether the hub starts the server itself or talks to one that is already running.",
        ),
        ConfigField(
            "command", "Command", required=False, show_if=(CONNECTION_KEY, "launch"),
            placeholder="npx -y @modelcontextprotocol/server-filesystem /data",
            help=(
                "Anything on npm (`npx -y <package>`) or PyPI (`uvx <package>`) — no registry "
                "of our own and nothing to install by hand. The server runs in its own process, "
                "so it cannot read credentials stored for other backends."
            ),
        ),
        ConfigField(
            "env", "Environment", type="textarea", secret=True, required=False,
            show_if=(CONNECTION_KEY, "launch"),
            placeholder="SOME_TOKEN=...\nANOTHER_SETTING=...",
            help="KEY=VALUE per line, passed to the launched server. Encrypted at rest.",
        ),
        ConfigField("url", "Upstream URL", required=False, show_if=(CONNECTION_KEY, "url"),
                    placeholder="http://192.168.1.50:8043/mcp",
                    help="The server's streamable-HTTP MCP endpoint, including the path."),
        ConfigField("auth_header", "Auth header name", required=False, show_if=(CONNECTION_KEY, "url"),
                    placeholder="Authorization",
                    help="Leave blank if the upstream needs no credentials."),
        ConfigField("auth_value", "Auth header value", type="password", secret=True, required=False,
                    show_if=(CONNECTION_KEY, "url"), placeholder="Bearer ...",
                    help="Sent verbatim as the header's value."),
        ConfigField("verify_tls", "Verify TLS certificate", type="bool", default=True, required=False,
                    show_if=(CONNECTION_KEY, "url"),
                    help="Turn off only for an https upstream with a self-signed certificate."),
        ConfigField(
            "ui_url", "Web interface", required=False, placeholder="http://10.0.0.50:8080",
            help=(
                "If this server also has a web interface, its address. Leave blank if it has none. "
                "Independent of how MCP itself is reached."
            ),
        ),
        ConfigField(
            "ui_proxy", "Serve the interface through the hub", type="bool", default=True,
            required=False, show_if=None,
            help=(
                "Puts it at /ui/<name>, behind this hub's sign-in and the same per-account grants "
                "as the MCP endpoint — so an interface with no login of its own gets one, without "
                "publishing another hostname. It runs sandboxed, in an origin of its own, which "
                "means an interface that has its own login cannot use its cookies through here. "
                "Turn this off to simply link to the address above instead."
            ),
        ),
        ConfigField(
            "ui_trusted", "This is an app I control", type="bool", default=False, required=False,
            help=(
                "Serves the interface on the hub's own origin instead of sandboxing it. "
                "An app with module scripts, its own login, or anything fetched with CORS "
                "needs this — a sandboxed page has an origin of its own, which makes every "
                "asset a cross-origin request and every cookie unavailable. "
                "The cost is real: on the hub's origin, the app's JavaScript can call the "
                "hub's own endpoints as whoever is signed in. Tick it only for an app you "
                "wrote or would trust with your administrator session. "
                "Trusted apps are also sent the signed-in account in X-Mcphub-User and its "
                "level on this backend in X-Mcphub-Role, so they can use this hub's accounts "
                "instead of having their own. The level is reported, not enforced: over MCP "
                "tools say what they do, over HTTP a POST is just a POST, so what a level "
                "means inside the app is the app's to decide."
            ),
        ),
        ConfigField("timeout", "Timeout (seconds)", type="number", default=30, required=False),
        ConfigField(
            storage.PER_VERSION, "Give each version its own storage", type="bool", default=False,
            help=(
                "Off by default: every version of this backend, and every account using it, "
                "shares one storage directory — which is what you want when the data is the "
                "thing people are working on together, and someone is trying a new version "
                "of the server that serves it. Turn it on for a backend whose versions keep "
                "something they cannot share, such as an index whose format changed. "
                "Note what this does not fix: two versions sharing a directory also share "
                "any schema migration one of them applies, and the hub cannot undo that."
            ),
        ),
        ConfigField(
            ALLOW_KEY, "Tools to expose", type="multiselect", required=False,
            help=(
                "Selecting none exposes everything the upstream offers, which for a large "
                "server is a lot of context spent before you ask anything. Narrow it to what "
                "you use."
            ),
        ),
    )

    def fields_for(self, instance: BackendInstance | None) -> tuple[ConfigField, ...]:
        """The generic form, with the upstream's own declared variables spliced in.

        Without this, a backend created from the registry loses everything the
        registry knew about it the moment it is saved: the typed, described
        fields collapse back to one freeform blob carrying an example about a
        different server entirely.
        """
        # Reflect how this backend is actually configured, so an existing
        # backend does not open on the wrong branch of the form.
        current = ""
        if instance is not None:
            current = str(instance.get(CONNECTION_KEY, "") or "")
            if not current:
                current = "launch" if str(instance.get("command", "") or "").strip() else "url"
        base = tuple(
            ConfigField(**{**f.__dict__, "default": current}) if f.key == CONNECTION_KEY and current else f
            for f in self.fields
        )

        declared = _declared_env(instance) if instance else []
        if not declared:
            return base

        typed = tuple(
            ConfigField(
                f"{ENV_PREFIX}{v['name']}",
                v["name"],
                # Everything declared is encrypted, not only what the upstream
                # flagged secret: which of its variables are sensitive is its
                # own claim, and a wrong claim should not leave a token in a
                # plaintext column.
                type="password" if v.get("isSecret") else "text",
                secret=True,
                # Displaying it back is the other question, and there the claim
                # is worth taking: encryption at rest guards a stolen database,
                # not the screen of the signed-in administrator who typed the
                # value in. Withholding everything meant a router's address and
                # its port reopened as empty boxes indistinguishable from a
                # backend that had never been configured at all.
                show_value=not v.get("isSecret"),
                required=bool(v.get("isRequired")),
                help=v.get("description", ""),
                show_if=(CONNECTION_KEY, "launch"),
            )
            for v in declared
        )

        out: list[ConfigField] = []
        for field in base:
            if field.key == "env":
                out.extend(typed)
                out.append(ConfigField(
                    "env", "Additional environment", type="textarea", secret=True, required=False,
                    show_if=(CONNECTION_KEY, "launch"),
                    placeholder="ANYTHING_ELSE=...",
                    help="KEY=VALUE per line, for variables this server did not declare.",
                ))
            else:
                out.append(field)
        return tuple(out)

    def validate(self, instance: BackendInstance) -> list[FieldError]:
        """Refuse the configurations that would otherwise fail quietly.

        Every one of these currently saves, mounts, and then does the wrong
        thing without saying so — which is a far worse way to find out than
        being told at the point you typed it.
        """
        problems: list[FieldError] = []
        command = str(instance.get("command", "") or "").strip()
        url = str(instance.get("url", "") or "").strip()
        # Mirrors `_upstream`, which is what actually decides. Validating
        # against a different rule than the one that runs would let through
        # exactly the configurations this is meant to stop.
        connection = str(instance.get(CONNECTION_KEY, "") or "") or ("launch" if command else "url")

        if connection == "launch" and not command:
            problems.append(FieldError(
                "Give the command that starts the server, or switch Connection to "
                "a server that is already running.", "command"))

        if connection == "url":
            parsed = urlparse(url)
            if not url:
                problems.append(FieldError(
                    "Give the URL of the running server, or switch Connection to "
                    "launching one here.", "url"))
            elif parsed.scheme not in ("http", "https") or not parsed.netloc:
                problems.append(FieldError(
                    f"{url!r} is not a complete URL. It needs a scheme and a host, "
                    "as in https://192.168.1.50:8043/mcp.", "url"))

            # `_upstream` sends the header only when it has both halves, so
            # half a credential is not a half-working backend — it is one
            # authenticating with nothing at all and not mentioning it.
            header = str(instance.get("auth_header", "") or "").strip()
            value = str(instance.get("auth_value", "") or "").strip()
            if value and not header:
                problems.append(FieldError(
                    "Name the header to send this value in, or it will not be sent "
                    "at all.", "auth_header"))
            if header and not value:
                problems.append(FieldError(
                    f"Give the value for the {header!r} header, or the header will "
                    "not be sent at all.", "auth_value"))

        # Read raw, not through the `or 30` that `_upstream` applies: that turns
        # a typed 0 into 30 and says nothing, which is the silent substitution
        # this hook exists to stop. Being stricter than the runtime is fine
        # here; being looser is what would let a bad config through.
        raw = instance.get("timeout", 30)
        try:
            if raw not in (None, "") and float(raw) <= 0:
                problems.append(FieldError(
                    "A timeout has to be greater than zero. Leave the box empty for "
                    "the default of 30 seconds.", "timeout"))
        except (TypeError, ValueError):  # pragma: no cover - refused before this
            pass

        return problems

    def build(self, instance: BackendInstance) -> MCPServer:
        upstream = _upstream(instance)
        if not upstream._cfg.url and not upstream._cfg.command:
            log.warning("backend %s has neither a command nor a URL", instance.slug)
        allow = _allowed(instance)
        catalog = _catalog(instance)

        mcp = MCPServer(
            name=f"proxy-{instance.slug}",
            title=instance.title,
            instructions=str(instance.config.get("upstream_instructions") or "") or None,
            version="0.1.0",
        )

        exposed = 0
        for tool in catalog:
            if allow is not None and tool.name not in allow:
                continue
            if mirror_tool(mcp, upstream, tool):
                exposed += 1

        # Resources are not filtered by the tool allowlist. A `ui://` resource
        # exists to be fetched by a tool that is exposed, and withholding it
        # would leave that tool pointing at an interface the client cannot load.
        resources = 0
        for resource in _resource_catalog(instance):
            if mirror_resource(mcp, upstream, resource):
                resources += 1
        if resources:
            log.info("backend %s proxies %d upstream resources", instance.slug, resources)

        prompts = sum(1 for p in _prompt_catalog(instance) if mirror_prompt(mcp, upstream, p))
        if prompts:
            log.info("backend %s proxies %d upstream prompts", instance.slug, prompts)

        if not catalog:
            log.warning(
                "backend %s has no cached tool catalogue; open its settings and save "
                "to fetch one", instance.slug,
            )
        log.info("backend %s proxies %d of %d upstream tools", instance.slug, exposed, len(catalog))
        return mcp

    async def check(self, instance: BackendInstance) -> CheckResult:
        if not str(instance.get("command", "") or "").strip() and not str(instance.get("url", "") or "").strip():
            return CheckResult(False, "Set either a command to launch, or the URL of a running server.")
        upstream = _upstream(instance)
        try:
            tools = await upstream.list_tools()
            info = await upstream.server_info()
            allow = _allowed(instance)
            shown = len(tools) if allow is None else len([t for t in tools if t.name in allow])
            name = info.get("name") or "upstream"
            version = info.get("version") or "?"
            how = "Launched" if upstream._cfg.is_stdio else "Connected to"
            return CheckResult(True, f"{how} {name} {version} — exposing {shown} of {len(tools)} tools")
        except UpstreamError as exc:
            return CheckResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim in the UI
            return CheckResult(False, f"{type(exc).__name__}: {exc}")
        finally:
            await upstream.close()

    def variant(self, instance: BackendInstance, version: str) -> BackendInstance:
        """This backend as it runs at `version`.

        The command is rebuilt with the version pinned, and the catalogue is
        swapped for the one read from that version. Nothing else about the
        backend changes, so the plugin's own code never learns about versions.
        """
        if not version:
            return instance

        package = instance.config.get(PACKAGE_KEY) or {}
        identifier = package.get("identifier")
        if not identifier:
            # Nothing reliable to pin: a hand-written command could be
            # anything, and guessing which token is the package would
            # eventually rewrite the wrong one.
            log.warning("backend %s has no package reference; ignoring the pin to %s",
                        instance.slug, version)
            return instance

        from dataclasses import replace

        from ....registry import PackageRef

        ref = PackageRef(
            registry_type=package.get("registryType", ""),
            identifier=identifier,
            runtime=package.get("runtime", ""),
            args=tuple(package.get("args") or ()),
        )
        catalogs = instance.config.get(VERSION_CATALOGS_KEY) or {}
        catalog = catalogs.get(version) or {}

        config = {
            **instance.config,
            "command": ref.command(version),
            CATALOG_KEY: catalog.get("tools") or "[]",
            RESOURCE_CATALOG_KEY: catalog.get("resources") or "[]",
            PROMPT_CATALOG_KEY: catalog.get("prompts") or "[]",
            NAME_KEY: catalog.get("name") or instance.config.get(NAME_KEY, ""),
            VERSION_KEY: version,
        }
        # `replace`, not a fresh instance: listing the fields by hand drops
        # any that were added since, silently and only at a pinned version.
        return replace(instance, config=config)

    def tool_names(self, instance: BackendInstance) -> set[str]:
        """What this backend currently believes the upstream offers."""
        return {t.name for t in _catalog(instance)}

    async def options(self, instance: BackendInstance, key: str) -> Sequence[Option]:
        if key != ALLOW_KEY:
            return ()
        upstream = _upstream(instance)
        try:
            tools = await upstream.list_tools()
        except Exception:  # noqa: BLE001 - the form still has to render
            log.info("backend %s: could not list upstream tools for the form", instance.slug)
            return ()
        finally:
            await upstream.close()
        return [
            Option(value=t.name, label=t.name, help=(t.description or "").split("\n")[0][:140])
            for t in sorted(tools, key=lambda t: t.name)
        ]

    async def on_save(self, instance: BackendInstance) -> dict[str, Any]:
        """Cache the upstream's tool schemas so `build` can stay offline."""
        upstream = _upstream(instance)
        try:
            tools = await upstream.list_tools()
            resources = await upstream.list_resources()
            prompts = await upstream.list_prompts()
            info = await upstream.server_info()
        except Exception:  # noqa: BLE001 - saving must succeed even if upstream is down
            log.info("backend %s: upstream unreachable at save, keeping the previous catalogue",
                     instance.slug)
            return {}
        finally:
            await upstream.close()
        return {
            # Recorded so the dashboard can show what is actually running, and
            # so a refresh can say what changed rather than just "done".
            NAME_KEY: info.get("name") or "",
            VERSION_KEY: info.get("version") or "",
            CATALOG_KEY: json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools]),
            RESOURCE_CATALOG_KEY: json.dumps(
                [r.model_dump(by_alias=True, exclude_none=True) for r in resources]
            ),
            PROMPT_CATALOG_KEY: json.dumps(
                [p.model_dump(by_alias=True, exclude_none=True) for p in prompts]
            ),
            "upstream_instructions": info.get("instructions") or "",
        }


PLUGIN = McpProxyPlugin()
