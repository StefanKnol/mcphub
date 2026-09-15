"""Mounting backends as live MCP endpoints.

Each enabled backend becomes its own ASGI app at ``/mcp/{slug}`` and is
registered in Claude as its own connector. They are deliberately *not* merged
into one endpoint: this MikroTik backend alone exposes two dozen tools and a
busy hub would reach several hundred, which is a large amount of context spent
before a single question is asked, and measurably worse tool selection.

Backends can be added and reconfigured while the server runs, so mounting has
to be dynamic. The awkward part is lifespans: Starlette does not run the
lifespan of a mounted sub-application, and the streamable-HTTP transport needs
its session manager running. Each mount therefore owns an AsyncExitStack that
is entered when it goes up and closed when it comes down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth.provider import ALL_SCOPES, SCOPE_USE, HubOAuthProvider
from .plugins.base import BackendInstance, Plugin

log = logging.getLogger(__name__)

MOUNT_PREFIX = "/mcp"

# Must be byte-identical to the issuer the authorization server advertises, so
# it is built the same way rather than through a plain AnyHttpUrl (which would
# append a trailing slash to a path-less URL). See mcphub.app.public_url.
_issuer_adapter = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


def _issuer_url(value: str) -> AnyHttpUrl:
    return _issuer_adapter.validate_python(value)


@dataclass
class Variant:
    """One version of a backend, running in its own process."""

    version: str
    server: MCPServer
    app: Any
    owner: asyncio.Task[None]
    stop: asyncio.Event


@dataclass
class Mounted:
    slug: str
    instance: BackendInstance
    plugin: Plugin
    resource_url: str
    routes: list[Any]
    variants: dict[str, Variant] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def server(self) -> MCPServer:
        """The default variant's server, for callers that just want one."""
        return self.variants[""].server


class MountManager:
    """Owns the live set of backend endpoints and keeps the router in sync."""

    def __init__(self, app: Any, provider: HubOAuthProvider, settings: Any, db: Any = None,
                 load_instance: Any = None) -> None:
        self._app = app
        self._provider = provider
        self._settings = settings
        self._db = db
        # A version's catalogue is written when someone pins it, which is after
        # the backend was mounted. Building a variant from the snapshot taken at
        # mount time would use config that predates the pin — and produce a
        # server with no tools at all.
        self._load_instance = load_instance
        self._public_url = settings.public_url.rstrip("/")
        self._verifier = ProviderTokenVerifier(provider)
        self._mounted: dict[str, Mounted] = {}

    def resource_url(self, slug: str) -> str:
        return f"{self._public_url}{MOUNT_PREFIX}/{slug}"

    def active(self) -> list[Mounted]:
        return sorted(self._mounted.values(), key=lambda m: m.slug)

    async def _start_variant(self, plugin: Plugin, instance: BackendInstance,
                             version: str) -> Variant:
        """Build and start one version's server. Same lifespan dance as a mount."""
        shaped = plugin.variant(instance, version) if version else instance
        server = plugin.build(shaped)
        sub_app = server.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=self._settings.allowed_hosts,
                allowed_origins=self._settings.allowed_origins,
            ),
        )

        stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def own() -> None:
            try:
                async with sub_app.router.lifespan_context(sub_app):
                    if not ready.done():
                        ready.set_result(None)
                    await stop.wait()
            except BaseException as exc:  # noqa: BLE001 - reported to the starter
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    log.exception("backend %s (%s) stopped unexpectedly",
                                  instance.slug, version or "default")

        owner = asyncio.create_task(own(), name=f"mount:{instance.slug}:{version or 'default'}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=30)
        except BaseException:
            stop.set()
            owner.cancel()
            raise
        return Variant(version=version, server=server, app=sub_app, owner=owner, stop=stop)

    def pin_for(self, username: str | None, slug: str) -> str:
        """The version this account chose for this backend, or "" for the default."""
        if not username or self._db is None:
            return ""
        row = self._db.one(
            "SELECT p.version FROM backend_pins p "
            "JOIN users u ON u.id = p.user_id JOIN backends b ON b.id = p.backend_id "
            "WHERE u.username = ? AND b.slug = ?",
            (username, slug),
        )
        return str(row["version"]) if row and row["version"] else ""

    async def variant(self, slug: str, version: str) -> Variant:
        """The running server for one version, started if this is its first caller."""
        mounted = self._mounted.get(slug)
        if mounted is None:
            raise LookupError(f"{slug} is not mounted")
        existing = mounted.variants.get(version)
        if existing is not None:
            return existing

        async with mounted.lock:
            # Another request may have started it while we waited.
            existing = mounted.variants.get(version)
            if existing is not None:
                return existing
            current = mounted.instance
            if self._load_instance is not None:
                fresh = self._load_instance(slug)
                if fresh is not None:
                    current = fresh
            started = await self._start_variant(mounted.plugin, current, version)
            mounted.variants[version] = started
            log.info("backend %s started at version %s", slug, version or "default")
            return started

    async def mount(self, plugin: Plugin, instance: BackendInstance) -> Mounted:
        """Bring a backend up, replacing any earlier mount of the same slug."""
        await self.unmount(instance.slug)

        resource_url = self.resource_url(instance.slug)
        default = await self._start_variant(plugin, instance, "")

        # Order outward: authenticate, require a token with the right scope,
        # check this account may use this backend, then route it to the version
        # it pinned. Nested the other way round, the check runs before anything
        # has authenticated and every request is a 401.
        #
        # `resource_server_url` is what pins a token to *this* backend: a token
        # minted for another backend on the same hub is refused, not honoured.
        dispatch = _VersionDispatch(self, instance.slug)
        authorized = _Authorized(dispatch, self._db, instance.slug) if self._db is not None else dispatch
        guarded = AuthenticationMiddleware(
            RequireAuthMiddleware(
                authorized,
                required_scopes=[SCOPE_USE],
                resource_metadata_url=build_resource_metadata_url(AnyHttpUrl(resource_url)),
            ),
            backend=BearerAuthBackend(self._verifier, resource_server_url=AnyHttpUrl(resource_url)),
            on_error=_auth_error,
        )

        # Not Starlette's Mount: its regex is `^/mcp/{slug}(?P<path>/.*)$`, so a
        # request to the bare endpoint — which is exactly the URL you paste into
        # a client — does not match, and falls through to the 404 handler.
        routes: list[Any] = [
            Route(
                f"{MOUNT_PREFIX}/{instance.slug}",
                endpoint=_AtRoot(guarded, f"{MOUNT_PREFIX}/{instance.slug}"),
                methods=["GET", "POST", "DELETE", "OPTIONS"],
            )
        ]
        # RFC 9728 puts resource metadata at the *root*, under a path derived
        # from the resource, so these cannot live inside the mounted app.
        routes += create_protected_resource_routes(
            resource_url=AnyHttpUrl(resource_url),
            authorization_servers=[_issuer_url(self._public_url)],
            scopes_supported=ALL_SCOPES,
            resource_name=instance.title,
        )

        mounted = Mounted(instance.slug, instance, plugin, resource_url, routes,
                          variants={"": default})
        self._mounted[instance.slug] = mounted
        self._insert_routes(routes)
        log.info("mounted backend %s (%s) at %s", instance.slug, plugin.id, resource_url)
        return mounted

    async def unmount(self, slug: str) -> None:
        mounted = self._mounted.pop(slug, None)
        if mounted is None:
            return
        self._remove_routes(mounted.routes)
        for variant in list(mounted.variants.values()):
            variant.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(variant.owner), timeout=15)
            except asyncio.TimeoutError:
                log.warning("backend %s (%s) did not shut down in time; cancelling",
                            slug, variant.version or "default")
                variant.owner.cancel()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - already unmounted either way
                log.exception("backend %s raised while shutting down", slug)
        log.info("unmounted backend %s (%d version(s))", slug, len(mounted.variants))

    async def unmount_all(self) -> None:
        for slug in list(self._mounted):
            await self.unmount(slug)

    # ── route table maintenance ───────────────────────────────────────────

    def _insert_routes(self, routes: list[Any]) -> None:
        """Insert ahead of the catch-all UI routes so mounts win the match."""
        table = self._app.router.routes
        index = next(
            (i for i, r in enumerate(table) if isinstance(r, Route) and getattr(r, "path", "") == "/{path:path}"),
            len(table),
        )
        for offset, route in enumerate(routes):
            table.insert(index + offset, route)

    def _remove_routes(self, routes: list[Any]) -> None:
        table = self._app.router.routes
        for route in routes:
            if route in table:
                table.remove(route)


class _VersionDispatch:
    """Send a request to the version the calling account pinned.

    Accounts may sit on different versions of the same backend, so one endpoint
    can front several running servers. Each is started the first time someone on
    that version connects, and a version nobody is using costs nothing.

    A pinned version can also offer a different set of tools, which is why this
    routes to a whole server rather than merely swapping a subprocess: offering
    an account a tool its own version does not have would fail only when it
    tried to call it.
    """

    def __init__(self, manager: "MountManager", slug: str) -> None:
        self._manager = manager
        self._slug = slug

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        user = scope.get("user")
        token = getattr(user, "access_token", None)
        version = self._manager.pin_for(getattr(token, "subject", None), self._slug)
        try:
            variant = await self._manager.variant(self._slug, version)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.exception("could not start %s at version %r", self._slug, version)
            await _unavailable(send, self._slug, version, exc)
            return
        await variant.app(scope, receive, send)


async def _unavailable(send: Send, slug: str, version: str, exc: Exception) -> None:
    body = json.dumps({
        "error": "backend_unavailable",
        "error_description": (
            f"{slug!r} could not be started at the pinned version {version!r}: {exc}. "
            "Clear the pin to use the backend's default version."
        ),
    }).encode()
    await send({"type": "http.response.start", "status": 503,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _Authorized:
    """Refuse a request from an account without a grant for this backend.

    Checked per request rather than when the token was issued, so removing an
    account's access takes effect at once instead of whenever its token happens
    to expire. A token proves who is asking; this decides whether they may.
    """

    def __init__(self, app: ASGIApp, db: Any, slug: str) -> None:
        self._app = app
        self._db = db
        self._slug = slug

    def _permitted(self, username: str | None) -> bool:
        if not username:
            return False
        row = self._db.one(
            "SELECT u.is_admin, "
            "       (SELECT COUNT(*) FROM backend_grants g JOIN backends b ON b.id = g.backend_id "
            "        WHERE g.user_id = u.id AND b.slug = ?) AS granted "
            "FROM users u WHERE u.username = ?",
            (self._slug, username),
        )
        if row is None:
            # The account was deleted while a token of theirs was still valid.
            return False
        return bool(row["is_admin"]) or bool(row["granted"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        user = scope.get("user")
        token = getattr(user, "access_token", None)
        username = getattr(token, "subject", None)
        if not self._permitted(username):
            log.warning("account %r has no grant for backend %s", username, self._slug)
            await _forbidden(send, self._slug)
            return
        await self._app(scope, receive, send)


async def _forbidden(send: Send, slug: str) -> None:
    body = json.dumps({
        "error": "access_denied",
        "error_description": f"This account has not been granted access to the {slug!r} backend.",
    }).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _AtRoot:
    """Present a sub-application with the root path, the way Mount would.

    The streamable-HTTP app routes its single endpoint at "/", but this route
    matched at /mcp/{slug}, so the path has to be rewritten before handing the
    request over — and `root_path` set, so anything the sub-app builds a URL
    from still sees where it really lives.
    """

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self._app = app
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope)
        scope["path"] = "/"
        scope["raw_path"] = b"/"
        scope["root_path"] = scope.get("root_path", "") + self._prefix
        await self._app(scope, receive, send)


async def _auth_error(conn: Any, exc: Exception):  # type: ignore[no-untyped-def]
    """Let RequireAuthMiddleware produce the 401 with its WWW-Authenticate header.

    Starlette's AuthenticationMiddleware would otherwise answer a malformed
    token with a bare 400 and no `WWW-Authenticate`, which is precisely the
    header a client needs in order to discover where to authenticate.
    """
    from starlette.responses import Response

    return Response(status_code=401)
