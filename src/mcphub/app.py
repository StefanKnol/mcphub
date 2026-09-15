"""ASGI application composition."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth.provider import ALL_SCOPES, HubOAuthProvider
from .config import Settings
from .crypto import SecretBox, hash_password, load_or_create_key
from .db import Database, utcnow
from .mounts import MountManager
from .plugins.base import BackendInstance
from .plugins.registry import PluginRegistry
from .web import routes as web_routes

log = logging.getLogger(__name__)

# RFC 8414 compares issuer strings exactly, and a bare `AnyHttpUrl` normalises
# "https://host" into "https://host/". A client that fetched the metadata from
# the un-slashed URL would then see an issuer that does not match what it asked
# for and reject the document. Preserving the empty path keeps them identical.
_URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


def public_url(value: str) -> AnyHttpUrl:
    return _URL.validate_python(value)


class Hub:
    """Everything the request handlers need, assembled once."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.secrets = SecretBox(load_or_create_key(settings.key_path))
        self.registry = PluginRegistry()
        self.registry.load_entry_points()
        self.provider = HubOAuthProvider(self.db)
        self.mounts: MountManager | None = None  # set once the app exists

    # ── backend instances ─────────────────────────────────────────────────

    def instance_from_row(self, row: Any) -> BackendInstance:
        import json

        return BackendInstance(
            slug=row["slug"],
            title=row["title"],
            plugin_id=row["plugin_id"],
            config=json.loads(row["config_json"]),
            secrets=self.secrets.open(row["secrets_blob"]),
        )

    def backend_rows(self, enabled_only: bool = False) -> list[Any]:
        sql = "SELECT * FROM backends"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.db.query(sql + " ORDER BY title")

    def backend_row(self, slug: str) -> Any | None:
        return self.db.one("SELECT * FROM backends WHERE slug = ?", (slug,))

    async def remount(self, slug: str) -> str | None:
        """Bring one backend's endpoint in line with its stored config.

        Returns an error string rather than raising: a backend that cannot be
        built should leave the rest of the hub serving, and the message belongs
        on the settings page next to the thing that caused it.
        """
        assert self.mounts is not None
        row = self.backend_row(slug)
        if row is None or not row["enabled"]:
            await self.mounts.unmount(slug)
            return None
        plugin = self.registry.get(row["plugin_id"])
        if plugin is None:
            await self.mounts.unmount(slug)
            return f"Plugin {row['plugin_id']!r} is not installed."
        try:
            await self.mounts.mount(plugin, self.instance_from_row(row))
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            log.exception("failed to mount backend %s", slug)
            return f"{type(exc).__name__}: {exc}"
        return None

    def bootstrap_admin(self) -> str | None:
        """Create the first account, returning its generated password once."""
        if self.db.one("SELECT id FROM users LIMIT 1"):
            return None
        password = secrets.token_urlsafe(18)
        self.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, 1, 1, ?)",
            ("admin", hash_password(password), utcnow()),
        )
        return password


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    hub = Hub(settings)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        password = hub.bootstrap_admin()
        if password:
            log.warning(
                "\n%s\nFirst run: created the 'admin' account.\n"
                "  username: admin\n  password: %s\n"
                "This is shown once. Sign in at %s and change it.\n%s",
                "=" * 68, password, settings.public_url, "=" * 68,
            )

        hub.mounts = MountManager(app, hub.provider, settings, hub.db)
        for row in hub.backend_rows(enabled_only=True):
            error = await hub.remount(row["slug"])
            if error:
                log.error("backend %s did not mount: %s", row["slug"], error)

        hub.db.purge_expired()
        try:
            yield
        finally:
            await hub.mounts.unmount_all()
            hub.db.close()

    issuer = public_url(settings.public_url)
    routes: list[Any] = list(
        create_auth_routes(
            provider=hub.provider,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,  # Dynamic client registration: how Claude registers itself.
                valid_scopes=ALL_SCOPES,
                default_scopes=ALL_SCOPES,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    )
    routes += web_routes.build(hub)
    routes.append(Route("/healthz", lambda r: JSONResponse({"ok": True})))
    # Catch-all last; MountManager inserts backend mounts directly above it.
    routes.append(Route("/{path:path}", web_routes.not_found))

    app = Starlette(routes=routes, lifespan=lifespan, debug=settings.dev_mode)
    app.state.hub = hub
    return app
