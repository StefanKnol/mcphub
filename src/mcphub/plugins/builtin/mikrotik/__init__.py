"""MikroTik RouterOS backend."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ...base import BackendInstance, CheckResult, ConfigField
from .client import RouterConfig, RouterError, RouterOS
from .tools import register

INSTRUCTIONS = (
    "Tools for one MikroTik RouterOS device.\n\n"
    "Every item the device holds has an `id` that looks like `*7`. Reads return "
    "it; writes require it. List results also carry a `position`, which is where "
    "the item currently sits in evaluation order — it is for reading, and it "
    "shifts whenever anything is added, removed or moved, so it is never a valid "
    "way to address a write.\n\n"
    "Firewall rules are evaluated in order and the first match wins, so when "
    "adding a rule, place it deliberately rather than letting it land at the end "
    "of a chain that terminates in a drop."
)


def _config(instance: BackendInstance) -> RouterConfig:
    return RouterConfig(
        host=str(instance.get("host", "")).strip(),
        username=str(instance.get("username", "")).strip(),
        password=str(instance.get("password", "")),
        port=int(instance.get("port", 8729) or 8729),
        use_tls=bool(instance.get("use_tls", True)),
        tls_fingerprint=str(instance.get("tls_fingerprint", "") or ""),
        timeout=float(instance.get("timeout", 10) or 10),
    )


class MikroTikPlugin:
    id = "mikrotik"
    name = "MikroTik RouterOS"
    description = (
        "Manage a MikroTik router over the RouterOS binary API: interfaces, "
        "addressing, firewall and NAT, DHCP, DNS, routes and logs."
    )

    fields = (
        ConfigField("host", "Host", help="IP address or hostname of the router.", placeholder="192.168.88.1"),
        ConfigField(
            "port", "API port", type="number", default=8729,
            help="8729 for the TLS API (api-ssl), 8728 for plaintext. Enable the service under IP > Services.",
        ),
        ConfigField(
            "use_tls", "Use TLS", type="bool", default=True, required=False,
            help="Strongly recommended. Plaintext on 8728 sends the router password over the network in the clear.",
        ),
        ConfigField("username", "Username", placeholder="mcp-agent",
                    help="Use a dedicated RouterOS user, not admin, so its access can be scoped and revoked on its own."),
        ConfigField("password", "Password", type="password", secret=True),
        ConfigField(
            "tls_fingerprint", "TLS fingerprint", required=False,
            help=(
                "Optional SHA-256 of the router's certificate. MikroTik's API-SSL certificate is "
                "self-signed, so normal CA validation cannot apply; pinning this is what makes the "
                "TLS connection meaningfully authenticated rather than merely encrypted."
            ),
            placeholder="ab:cd:ef:...",
        ),
        ConfigField("timeout", "Timeout (seconds)", type="number", default=10, required=False),
    )

    def build(self, instance: BackendInstance) -> MCPServer:
        mcp = MCPServer(
            name=f"mikrotik-{instance.slug}",
            title=instance.title,
            instructions=INSTRUCTIONS,
            version="0.1.0",
        )
        register(mcp, RouterOS(_config(instance)), title=instance.title)
        return mcp

    async def check(self, instance: BackendInstance) -> CheckResult:
        cfg = _config(instance)
        if not cfg.host or not cfg.username:
            return CheckResult(False, "Host and username are required.")
        router = RouterOS(cfg)
        try:
            identity = await router.list("system", "identity")
            resource = await router.list("system", "resource")
            name = identity[0].get("name", "?") if identity else "?"
            version = resource[0].get("version", "?") if resource else "?"
            return CheckResult(True, f"Connected to {name} — RouterOS {version}")
        except RouterError as exc:
            return CheckResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim in the UI
            return CheckResult(False, f"{type(exc).__name__}: {exc}")
        finally:
            await router.close()


PLUGIN = MikroTikPlugin()
