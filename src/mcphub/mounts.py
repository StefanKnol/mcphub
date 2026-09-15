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
from dataclasses import dataclass
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
class Mounted:
    slug: str
    instance: BackendInstance
    server: MCPServer
    resource_url: str
    routes: list[Any]
    owner: asyncio.Task[None]
    stop: asyncio.Event


class MountManager:
    """Owns the live set of backend endpoints and keeps the router in sync."""

    def __init__(self, app: Any, provider: HubOAuthProvider, settings: Any, db: Any = None) -> None:
        self._app = app
        self._provider = provider
        self._settings = settings
        self._db = db
        self._public_url = settings.public_url.rstrip("/")
        self._verifier = ProviderTokenVerifier(provider)
        self._mounted: dict[str, Mounted] = {}

    def resource_url(self, slug: str) -> str:
        return f"{self._public_url}{MOUNT_PREFIX}/{slug}"

    def active(self) -> list[Mounted]:
        return sorted(self._mounted.values(), key=lambda m: m.slug)

    async def mount(self, plugin: Plugin, instance: BackendInstance) -> Mounted:
        """Bring a backend up, replacing any earlier mount of the same slug."""
        await self.unmount(instance.slug)

        server = plugin.build(instance)
        resource_url = self.resource_url(instance.slug)

        # `streamable_http_path="/"` because the mount prefix already carries
        # the path; the SDK would otherwise serve at /mcp/{slug}/mcp.
        #
        # `transport_security` is not optional in practice: without it the SDK
        # accepts only a 127.0.0.1 Host header, so behind a reverse proxy every
        # MCP request is rejected with 421 after OAuth has already succeeded.
        sub_app = server.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=self._settings.allowed_hosts,
                allowed_origins=self._settings.allowed_origins,
            ),
        )

        # The sub-app's lifespan is the streamable-HTTP session manager, which
        # opens an anyio task group. A task group must be exited by the task
        # that entered it, and mounts are torn down from request handlers while
        # they are first set up during startup — different tasks. So each mount
        # gets an owner task that holds its lifespan open for as long as it
        # lives, and unmounting asks that task to finish.
        stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def own() -> None:
            try:
                async with sub_app.router.lifespan_context(sub_app):
                    if not ready.done():
                        ready.set_result(None)
                    await stop.wait()
            except BaseException as exc:  # noqa: BLE001 - reported to mount()
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    log.exception("backend %s stopped unexpectedly", instance.slug)

        owner = asyncio.create_task(own(), name=f"mount:{instance.slug}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=30)
        except BaseException:
            stop.set()
            owner.cancel()
            raise

        # Order matters and is easy to get backwards: AuthenticationMiddleware
        # is what puts the authenticated user on the scope, so it has to be the
        # *outer* layer. RequireAuthMiddleware then reads that and enforces the
        # scope, answering with the WWW-Authenticate header that tells a client
        # where to authenticate. Nested the other way round, the check runs
        # before anything has authenticated and every request is a 401.
        #
        # `resource_server_url` is what pins a token to *this* backend: a token
        # minted for another backend on the same hub is refused, not honoured.
        # Order outward: authenticate, require a token with the right scope,
        # then check this particular account may use this particular backend.
        authorized = _Authorized(sub_app, self._db, instance.slug) if self._db is not None else sub_app
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
        # a client — does not match, and falls through to the 404 handler. The
        # endpoint is a single URL, so match it exactly and hand the sub-app the
        # root path it expects.
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

        mounted = Mounted(instance.slug, instance, server, resource_url, routes, owner, stop)
        self._mounted[instance.slug] = mounted
        self._insert_routes(routes)
        log.info("mounted backend %s (%s) at %s", instance.slug, plugin.id, resource_url)
        return mounted

    async def unmount(self, slug: str) -> None:
        mounted = self._mounted.pop(slug, None)
        if mounted is None:
            return
        self._remove_routes(mounted.routes)
        mounted.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(mounted.owner), timeout=15)
        except asyncio.TimeoutError:
            log.warning("backend %s did not shut down in time; cancelling", slug)
            mounted.owner.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - already unmounted either way
            log.exception("backend %s raised while shutting down", slug)
        log.info("unmounted backend %s", slug)

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
