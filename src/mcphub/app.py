"""ASGI application composition."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import cors_middleware
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth.provider import ALL_SCOPES, HubOAuthProvider
from .config import Settings
from .auth.cimd import ClientMetadataResolver
from .crypto import SecretBox, hash_password, load_or_create_key
from .db import Database, utcnow
from .mounts import MountManager
from . import storage
from .plugins.base import BackendInstance
from .plugins.registry import PluginRegistry
from .updates import UpdateChecker
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
        self.client_metadata = ClientMetadataResolver(enabled=settings.cimd_enabled)
        self.provider = HubOAuthProvider(self.db, client_metadata=self.client_metadata)
        self.mounts: MountManager | None = None  # set once the app exists
        self.updates = UpdateChecker(self, settings.update_interval)

    # ── backend instances ─────────────────────────────────────────────────

    def instance_from_row(self, row: Any) -> BackendInstance:
        import json

        return BackendInstance(
            slug=row["slug"],
            title=row["title"],
            plugin_id=row["plugin_id"],
            config=json.loads(row["config_json"]),
            secrets=self.secrets.open(row["secrets_blob"]),
            # The path, not the directory: reading a backend happens on every
            # page render, and creating it belongs where it is about to be
            # used — when the backend is saved, and when it is mounted.
            storage=storage.path_for(self.settings.data_dir, row["slug"]),
        )

    def current_instance(self, slug: str) -> BackendInstance | None:
        """This backend as it is stored right now, not as it was when mounted."""
        row = self.backend_row(slug)
        return self.instance_from_row(row) if row is not None else None

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

        hub.mounts = MountManager(app, hub.provider, settings, hub.db,
                                  load_instance=hub.current_instance)
        for row in hub.backend_rows(enabled_only=True):
            error = await hub.remount(row["slug"])
            if error:
                log.error("backend %s did not mount: %s", row["slug"], error)

        hub.db.purge_expired()
        if settings.update_interval > 0:
            hub.updates.start()
        try:
            yield
        finally:
            await hub.updates.stop()
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
    if settings.cimd_enabled:
        # The SDK builds this metadata inside create_auth_routes and has no way
        # to add the flag, so the document is rebuilt and its route replaced.
        # Without the flag a client has no way to know it may present a URL,
        # and falls back to registering.
        registration = ClientRegistrationOptions(
            enabled=True, valid_scopes=ALL_SCOPES, default_scopes=ALL_SCOPES)
        metadata = build_metadata(issuer, None, registration, RevocationOptions(enabled=True))
        metadata.client_id_metadata_document_supported = True
        routes[0] = Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )

    routes += web_routes.build(hub)
    routes.append(Route("/healthz", lambda r: JSONResponse({"ok": True})))
    # Catch-all last; MountManager inserts backend mounts directly above it.
    routes.append(Route("/{path:path}", web_routes.not_found))

    app = Starlette(routes=routes, lifespan=lifespan, debug=settings.dev_mode)
    app.state.hub = hub
    return app
