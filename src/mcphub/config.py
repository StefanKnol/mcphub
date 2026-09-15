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
    """Relaxes the HTTPS requirement on the public URL. Never enable in production."""

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

        if not dev_mode and not public_url.startswith("https://"):
            localhost = public_url.startswith(("http://localhost", "http://127.0.0.1"))
            if not localhost:
                raise ValueError(
                    f"MCPHUB_PUBLIC_URL must be https:// (got {public_url!r}). "
                    "OAuth bearer tokens would otherwise cross the network in the clear. "
                    "Set MCPHUB_DEV=1 to override for local development only."
                )

        return cls(
            data_dir=_env_path("MCPHUB_DATA_DIR", "/data"),
            public_url=public_url,
            host=os.environ.get("MCPHUB_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCPHUB_PORT", "8080")),
            dev_mode=dev_mode,
        )
