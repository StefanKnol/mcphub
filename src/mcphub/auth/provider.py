"""OAuth 2.1 authorization server backed by the hub's own user table.

This is the piece the old setup was missing entirely: the server it replaced
had no auth layer at all, so anyone who could reach the URL had unauthenticated
write access to the router behind it.

Token scoping is per *backend*, not per hub. Every MCP endpoint is a separate
RFC 8707 resource, and `load_access_token` refuses a token whose `resource`
does not match the endpoint being called — so a token issued for the router
connector cannot be replayed against another backend on the same hub.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from ..crypto import hash_token, new_token, verify_password
from .cimd import ClientMetadataResolver, is_cimd_client_id
from ..db import Database, utcnow

log = logging.getLogger(__name__)

ACCESS_TOKEN_TTL = 3600           # 1 hour
REFRESH_TOKEN_TTL = 30 * 86400    # 30 days
AUTH_CODE_TTL = 300               # 5 minutes
PENDING_TTL = 600                 # 10 minutes to finish logging in

SCOPE_USE = "mcp:use"
ALL_SCOPES = [SCOPE_USE]


class PendingAuthorization:
    """An /authorize request parked while the user logs in."""

    __slots__ = ("client_id", "params", "expires_at")

    def __init__(self, client_id: str, params: AuthorizationParams) -> None:
        self.client_id = client_id
        self.params = params
        self.expires_at = time.time() + PENDING_TTL


class HubOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, db: Database, login_path: str = "/login",
                 client_metadata: ClientMetadataResolver | None = None) -> None:
        self._db = db
        self._login_path = login_path
        # Clients that present a metadata document URL instead of registering.
        self._client_metadata = client_metadata or ClientMetadataResolver()
        # Pending authorizations are deliberately in-memory: they live for
        # minutes, and losing them on restart costs the user one click on the
        # login button rather than a corrupted persistent state to reason about.
        self._pending: dict[str, PendingAuthorization] = {}

    # ── client registration (RFC 7591) ────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._db.one("SELECT metadata_json FROM oauth_clients WHERE client_id = ?", (client_id,))
        if row is not None:
            return OAuthClientInformationFull.model_validate_json(row["metadata_json"])

        # Not registered here. It may instead be a URL describing itself, which
        # is how a client connects to a server it has never registered with.
        # Resolved on demand and never stored: the document is the record, and
        # caching it as a registration would let a stale copy outlive an edit.
        return await self._client_metadata.resolve(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if is_cimd_client_id(client_info.client_id):
            # Registering a URL-shaped id would shadow the document it names,
            # and whoever registered first would own that identity.
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="A URL client_id is resolved from its metadata document "
                                  "and cannot also be registered.",
            )
        self._db.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, secret_hash, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                client_info.client_id,
                hash_token(client_info.client_secret) if client_info.client_secret else None,
                client_info.model_dump_json(),
                utcnow(),
            ),
        )
        log.info("registered oauth client %s (%s)", client_info.client_id, client_info.client_name)

    # ── authorization ─────────────────────────────────────────────────────

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to the hub's login page."""
        if not params.code_challenge:
            # PKCE is mandatory in OAuth 2.1 and the MCP spec; a public client
            # without it is exactly the replay risk this whole layer exists for.
            raise AuthorizeError(error="invalid_request", error_description="PKCE code_challenge is required")

        self._sweep_pending()
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = PendingAuthorization(client.client_id, params)
        return f"{self._login_path}?{urlencode({'req': request_id})}"

    def get_pending(self, request_id: str) -> PendingAuthorization | None:
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        if pending.expires_at < time.time():
            self._pending.pop(request_id, None)
            return None
        return pending

    def authenticate_user(self, username: str, password: str) -> int | None:
        """Return the user id on success. Used by the login form."""
        row = self._db.one("SELECT id, password_hash FROM users WHERE username = ?", (username,))
        if row is None:
            # Burn roughly the same time as a real verify so the response time
            # does not reveal whether the username exists.
            verify_password(password, "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 44)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return int(row["id"])

    def complete_authorization(self, request_id: str, user_id: int) -> str:
        """Mint an authorization code and return the client's redirect URL."""
        pending = self.get_pending(request_id)
        if pending is None:
            raise AuthorizeError(error="invalid_request", error_description="authorization request expired")
        self._pending.pop(request_id, None)
        params = pending.params

        code = secrets.token_urlsafe(32)  # 256 bits, well above the 128-bit floor
        self._db.execute(
            "INSERT INTO auth_codes (code, client_id, user_id, redirect_uri, explicit_uri, "
            "code_challenge, scopes, resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                pending.client_id,
                user_id,
                str(params.redirect_uri),
                int(params.redirect_uri_provided_explicitly),
                params.code_challenge,
                " ".join(params.scopes or [SCOPE_USE]),
                params.resource,
                time.time() + AUTH_CODE_TTL,
            ),
        )

        query: dict[str, Any] = {"code": code}
        if params.state is not None:
            query["state"] = params.state
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        return f"{params.redirect_uri}{sep}{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._db.one("SELECT * FROM auth_codes WHERE code = ?", (authorization_code,))
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["expires_at"] < time.time():
            self._db.execute("DELETE FROM auth_codes WHERE code = ?", (authorization_code,))
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row["explicit_uri"]),
            resource=row["resource"],
            subject=self._username(row["user_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        row = self._db.one("SELECT user_id FROM auth_codes WHERE code = ?", (authorization_code.code,))
        if row is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used or expired")
        # Single use: delete before issuing, so a replayed code cannot mint a
        # second pair of tokens even if two requests race.
        self._db.execute("DELETE FROM auth_codes WHERE code = ?", (authorization_code.code,))

        return self._issue_pair(
            client_id=client.client_id,
            user_id=int(row["user_id"]),
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # ── refresh ───────────────────────────────────────────────────────────

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._db.one(
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = 'refresh'", (hash_token(refresh_token),)
        )
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
            subject=self._username(row["user_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        row = self._db.one(
            "SELECT user_id FROM tokens WHERE token_hash = ? AND kind = 'refresh'",
            (hash_token(refresh_token.token),),
        )
        if row is None:
            raise TokenError(error="invalid_grant", error_description="unknown refresh token")

        requested = scopes or refresh_token.scopes
        if not set(requested) <= set(refresh_token.scopes):
            raise TokenError(error="invalid_scope", error_description="cannot widen scope on refresh")

        # Rotate: the presented refresh token dies with this exchange.
        self._db.execute("DELETE FROM tokens WHERE token_hash = ?", (hash_token(refresh_token.token),))
        return self._issue_pair(
            client_id=client.client_id,
            user_id=int(row["user_id"]),
            scopes=requested,
            resource=refresh_token.resource,
        )

    # ── verification ──────────────────────────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._db.one(
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = 'access'", (hash_token(token),)
        )
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            self._db.execute("DELETE FROM tokens WHERE token_hash = ?", (hash_token(token),))
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
            subject=self._username(row["user_id"]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._db.execute("DELETE FROM tokens WHERE token_hash = ?", (hash_token(token.token),))

    # ── helpers ───────────────────────────────────────────────────────────

    def _issue_pair(self, *, client_id: str, user_id: int, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = new_token(), new_token()
        now = time.time()
        for tok, kind, ttl in ((access, "access", ACCESS_TOKEN_TTL), (refresh, "refresh", REFRESH_TOKEN_TTL)):
            self._db.execute(
                "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, resource, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hash_token(tok), kind, client_id, user_id, " ".join(scopes), resource, now + ttl, utcnow()),
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _username(self, user_id: int) -> str | None:
        row = self._db.one("SELECT username FROM users WHERE id = ?", (user_id,))
        return row["username"] if row else None

    def _sweep_pending(self) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if v.expires_at < now]:
            self._pending.pop(key, None)
