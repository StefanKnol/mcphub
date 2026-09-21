"""Client ID Metadata Documents: a client_id that is a URL describing the client.

Instead of registering (RFC 7591), a client presents an HTTPS URL as its
`client_id`, and the authorization server fetches the client metadata from
there. Nothing is stored ahead of time and nothing is issued — which is why a
client like Claude can connect to a server it has never met, provided the
server supports this.

The security shape is inverted from DCR and worth stating plainly: an
**unauthenticated** caller hands us a URL and we make an outbound request to
it. That is a server-side request forgery primitive unless it is fenced, so:

- HTTPS only, and a non-root path, per the spec's own client-side validator.
- Every resolved address is checked, and anything private, loopback,
  link-local or otherwise not a public unicast address is refused. This hub
  typically sits on a LAN with a router on it; `https://10.0.0.1/x` must not
  become a way to make the hub probe it.
- Redirects are not followed. A public URL that redirects to a private one is
  the usual way around an address check.
- The body is capped, the timeout is short, and both hits and misses are
  cached so a repeated client_id is not a repeated fetch.

No credentials, cookies or hub state are ever sent with the request.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx2
from mcp.shared.auth import OAuthClientInformationFull

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
FETCH_TIMEOUT = 5.0
CACHE_TTL = 900
NEGATIVE_TTL = 60
"""Failures are cached too, briefly, so a bad client_id cannot be replayed into
an outbound request per attempt."""


class CimdError(ValueError):
    """The URL or the document it serves is not usable as a client."""


def is_cimd_client_id(client_id: str | None) -> bool:
    """Whether this client_id is a metadata document URL rather than a registration.

    Matches the client-side rule in the SDK: HTTPS, with a path that is not
    just "/". A bare origin is rejected so an ordinary website cannot become a
    client id by accident.
    """
    if not client_id:
        return False
    try:
        parsed = urlparse(client_id)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.path not in ("", "/")


def _public_addresses(host: str) -> list[str]:
    """Resolve a host, refusing anything that is not a public unicast address."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise CimdError(f"{host} could not be resolved") from exc

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified):
            raise CimdError(
                f"{host} resolves to {address}, which is not a public address. "
                "A client id must be a document on the public internet."
            )
        addresses.append(address)
    if not addresses:
        raise CimdError(f"{host} has no usable address")
    return addresses


@dataclass
class _Entry:
    client: OAuthClientInformationFull | None
    error: str | None
    expires_at: float


class ClientMetadataResolver:
    """Fetches and caches client metadata documents."""

    def __init__(self, enabled: bool = True, transport: Any = None,
                 resolve_addresses: Any = None) -> None:
        self.enabled = enabled
        self._cache: dict[str, _Entry] = {}
        # Seams for tests, so the address check and the network can be exercised
        # separately. Production passes neither, and the defaults are the strict
        # behaviour — there is no switch here that turns a check off.
        self._transport = transport
        self._resolve = resolve_addresses or _public_addresses

    async def resolve(self, client_id: str) -> OAuthClientInformationFull | None:
        if not self.enabled or not is_cimd_client_id(client_id):
            return None

        now = time.time()
        cached = self._cache.get(client_id)
        if cached and cached.expires_at > now:
            if cached.error:
                log.debug("cimd %s: %s (cached)", client_id, cached.error)
                return None
            return cached.client

        try:
            client = await self._fetch(client_id)
        except CimdError as exc:
            log.info("refusing client id %s: %s", client_id, exc)
            self._cache[client_id] = _Entry(None, str(exc), now + NEGATIVE_TTL)
            return None
        except Exception as exc:  # noqa: BLE001 - never let a bad document 500 the flow
            log.warning("client id %s could not be read: %s", client_id, exc)
            self._cache[client_id] = _Entry(None, str(exc), now + NEGATIVE_TTL)
            return None

        self._cache[client_id] = _Entry(client, None, now + CACHE_TTL)
        return client

    async def _fetch(self, client_id: str) -> OAuthClientInformationFull:
        parsed = urlparse(client_id)
        await anyio.to_thread.run_sync(self._resolve, parsed.hostname or "")

        async with httpx2.AsyncClient(
            timeout=FETCH_TIMEOUT,
            # A public URL redirecting to a private one is the usual way past an
            # address check, so no redirect is followed at all.
            follow_redirects=False,
            **({"transport": self._transport} if self._transport is not None else {}),
        ) as client:
            response = await client.get(client_id, headers={"Accept": "application/json"})

        if response.status_code != 200:
            raise CimdError(f"the document returned {response.status_code}")
        body = response.content[: MAX_BODY_BYTES + 1]
        if len(body) > MAX_BODY_BYTES:
            raise CimdError(f"the document is larger than {MAX_BODY_BYTES} bytes")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CimdError("the document is not JSON") from exc
        if not isinstance(payload, dict):
            raise CimdError("the document is not a JSON object")

        return self._to_client(client_id, payload)

    @staticmethod
    def _to_client(client_id: str, payload: dict[str, Any]) -> OAuthClientInformationFull:
        """Build client information, with the URL as the authority on identity.

        A document claiming a different `client_id` than the URL it was served
        from is refused rather than trusted: that is how one client would
        impersonate another.
        """
        declared = payload.get("client_id")
        if declared is not None and str(declared) != client_id:
            raise CimdError(
                f"the document claims client_id {declared!r}, but it was fetched from {client_id!r}"
            )

        data = {k: v for k, v in payload.items() if k not in {"issuer", "client_secret"}}
        data["client_id"] = client_id
        # A document cannot confer a secret on itself; anyone who can read the
        # URL could then authenticate as it. CIMD clients are public clients,
        # which is why PKCE carries the weight here.
        data["token_endpoint_auth_method"] = "none"

        try:
            client = OAuthClientInformationFull.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - pydantic's message is the useful part
            raise CimdError(f"the document is not valid client metadata: {exc}") from exc

        if not client.redirect_uris:
            raise CimdError("the document declares no redirect_uris")
        return client
