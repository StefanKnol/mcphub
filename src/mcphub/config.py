"""Bootstrap settings.

Deliberately tiny. Environment variables configure *where the server keeps its
state* and nothing else — every backend, credential and user lives in the
database and is edited through the web UI. This is the whole point of the
rewrite: adding a router must not mean editing a YAML file and restarting a
container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    """Holds hub.db and master.key. Mount this as a volume."""

    public_url: str
    """Externally reachable origin, e.g. https://mcp.example.com.

    OAuth issuer and resource identifiers are derived from this, and RFC 8414
    requires the issuer in metadata to match byte-for-byte what the client
    fetched. Behind a reverse proxy this must be the *proxy's* URL, not the
    container's, or discovery fails in ways that are tedious to debug.
    """

    host: str
    port: int
    dev_mode: bool
    cimd_enabled: bool = True
    """Accept a client_id that is an HTTPS URL describing the client, instead of
    requiring registration. Turning it off means the hub never makes an outbound
    request to a URL an unauthenticated caller supplied, at the cost of only
    working with clients that register."""

    update_interval: int = 3600
    """Seconds between registry update checks. 0 disables the background check
    entirely; the floor is enforced in UpdateChecker so a small value cannot
    become a hot loop against someone else's service."""
    """Relaxes the HTTPS requirement on the public URL. Never enable in production."""

    @property
    def allowed_hosts(self) -> list[str]:
        """Host header values the MCP transport will accept.

        The SDK enforces DNS-rebinding protection on its HTTP transports and,
        left to its defaults, trusts only 127.0.0.1. Behind a reverse proxy the
        Host header is the public name, so every MCP request comes back
        421 Misdirected Request *after* a completely successful OAuth flow —
        which looks like an authentication problem and is not one.

        Protection stays on; it is simply told the names this hub is reached by.
        """
        hosts: set[str] = set()
        parsed = urlparse(self.public_url)
        if parsed.hostname:
            hosts.add(parsed.hostname)
            hosts.add(parsed.netloc)
            # Some proxies pass the default port through explicitly.
            hosts.add(f"{parsed.hostname}:{443 if parsed.scheme == 'https' else 80}")
        # The container's own address, so direct access on the LAN still works.
        hosts.update({"localhost", "127.0.0.1", f"localhost:{self.port}", f"127.0.0.1:{self.port}"})
        hosts.update(h.strip() for h in os.environ.get("MCPHUB_ALLOWED_HOSTS", "").split(",") if h.strip())
        return sorted(hosts)

    @property
    def allowed_origins(self) -> list[str]:
        """Origin values accepted alongside the hosts, for browser-based clients."""
        parsed = urlparse(self.public_url)
        origins = {self.public_url}
        if parsed.hostname:
            origins.add(f"{parsed.scheme}://{parsed.hostname}")
        origins.update({f"http://localhost:{self.port}", f"http://127.0.0.1:{self.port}"})
        return sorted(origins)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "master.key"

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = os.environ.get("MCPHUB_PUBLIC_URL", "http://localhost:8080").rstrip("/")
        dev_mode = os.environ.get("MCPHUB_DEV", "").lower() in {"1", "true", "yes"}

        # http is accepted only on loopback, and MCPHUB_DEV does not change that:
        # the MCP SDK rejects any non-HTTPS OAuth issuer outright, so allowing it
        # here would only swap this message for a stack trace at startup.
        if not public_url.startswith("https://"):
            if not public_url.startswith(("http://localhost", "http://127.0.0.1")):
                raise ValueError(
                    f"MCPHUB_PUBLIC_URL must be https:// (got {public_url!r}).\n"
                    "  OAuth requires an HTTPS issuer, and bearer tokens would otherwise "
                    "cross the network in the clear.\n"
                    "  Only http://localhost and http://127.0.0.1 are accepted, for local "
                    "development. Behind a reverse proxy, set this to the proxy's https URL."
                )

        return cls(
            data_dir=_env_path("MCPHUB_DATA_DIR", "/data"),
            public_url=public_url,
            host=os.environ.get("MCPHUB_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCPHUB_PORT", "8080")),
            dev_mode=dev_mode,
            cimd_enabled=os.environ.get("MCPHUB_CIMD", "1").lower() not in {"0", "false", "no"},
            update_interval=int(os.environ.get("MCPHUB_UPDATE_INTERVAL", "3600")),
        )
