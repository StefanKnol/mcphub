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

from mcp.server.mcpserver import MCPServer
from mcp.types import Tool as UpstreamTool

from ...base import BackendInstance, CheckResult, ConfigField, Option, PluginDefaults
from .mirror import mirror_tool
from .upstream import Upstream, UpstreamConfig, UpstreamError

log = logging.getLogger(__name__)

CATALOG_KEY = "tool_catalog"
ALLOW_KEY = "tools"


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


def _upstream(instance: BackendInstance) -> Upstream:
    headers: dict[str, str] = {}
    header_name = str(instance.get("auth_header", "") or "").strip()
    header_value = str(instance.get("auth_value", "") or "").strip()
    if header_name and header_value:
        headers[header_name] = header_value
    return Upstream(UpstreamConfig(
        url=str(instance.get("url", "") or "").strip(),
        command=str(instance.get("command", "") or "").strip(),
        env=_parse_env(str(instance.get("env", "") or "")),
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

    fields = (
        ConfigField(
            "command", "Command", required=False,
            placeholder="npx -y @modelcontextprotocol/server-filesystem /data",
            help=(
                "Have the hub launch the server itself. Use this for anything on npm "
                "(`npx -y <package>`) or PyPI (`uvx <package>`) — no registry of our own "
                "and nothing to install by hand. The server runs in its own process, so "
                "it cannot read credentials stored for other backends. "
                "Leave blank to connect to a server that is already running, below."
            ),
        ),
        ConfigField(
            "env", "Environment", type="textarea", secret=True, required=False,
            placeholder="GITHUB_TOKEN=ghp_...\nBRAVE_API_KEY=...",
            help=(
                "KEY=VALUE per line, passed to the launched server. Most published servers "
                "take their API key this way. Encrypted at rest and never shown again."
            ),
        ),
        ConfigField("url", "Upstream URL", required=False, placeholder="http://192.168.1.50:8043/mcp",
                    help="For a server that is already running: its streamable-HTTP MCP endpoint, including the path. Ignored when a command is set."),
        ConfigField("auth_header", "Auth header name", required=False, placeholder="Authorization",
                    help="Leave blank if the upstream needs no credentials."),
        ConfigField("auth_value", "Auth header value", type="password", secret=True, required=False,
                    placeholder="Bearer ...", help="Sent verbatim as the header's value."),
        ConfigField("verify_tls", "Verify TLS certificate", type="bool", default=True, required=False,
                    help="Turn off only for an https upstream with a self-signed certificate."),
        ConfigField("timeout", "Timeout (seconds)", type="number", default=30, required=False),
        ConfigField(
            ALLOW_KEY, "Tools to expose", type="multiselect", required=False,
            help=(
                "Selecting none exposes everything the upstream offers, which for a large "
                "server is a lot of context spent before you ask anything. Narrow it to what "
                "you use."
            ),
        ),
    )

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
            info = await upstream.server_info()
        except Exception:  # noqa: BLE001 - saving must succeed even if upstream is down
            log.info("backend %s: upstream unreachable at save, keeping the previous catalogue",
                     instance.slug)
            return {}
        finally:
            await upstream.close()
        return {
            CATALOG_KEY: json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools]),
            "upstream_instructions": info.get("instructions") or "",
        }


PLUGIN = McpProxyPlugin()
