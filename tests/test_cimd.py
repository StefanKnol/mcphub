"""Client ID Metadata Documents.

A client presents an HTTPS URL as its `client_id` and the server fetches the
client metadata from it, so a client can connect to a server it has never
registered with. The security shape is the inverse of registration: an
*unauthenticated* caller hands us a URL and we make an outbound request to it,
which is a request-forgery primitive unless it is fenced.
"""

import json

import httpx2
import pytest

from mcphub.auth.cimd import (
    MAX_BODY_BYTES,
    CimdError,
    ClientMetadataResolver,
    _public_addresses,
    is_cimd_client_id,
)

URL = "https://client.example.com/mcp/metadata"
DOCUMENT = {
    "client_name": "Some Client",
    "redirect_uris": ["https://client.example.com/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}


def resolver(handler, **kwargs) -> ClientMetadataResolver:
    return ClientMetadataResolver(
        transport=httpx2.MockTransport(handler),
        # The address check is exercised on its own; here it is stubbed so the
        # fetch can be tested without a public host.
        resolve_addresses=lambda host: ["203.0.113.1"],
        **kwargs,
    )


def serving(payload, status=200, headers=None):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return lambda request: httpx2.Response(status, content=body, headers=headers or {})


# ── which client ids are documents ────────────────────────────────────────

@pytest.mark.parametrize("client_id,expected", [
    ("https://claude.ai/some/path", True),
    ("https://example.com/", False),      # a bare origin is not a document
    ("https://example.com", False),
    ("http://example.com/doc", False),    # plaintext is refused outright
    ("9f8e7d6c-uuid-style", False),
    ("", False),
    (None, False),
])
def test_recognising_a_document_client_id(client_id, expected):
    assert is_cimd_client_id(client_id) is expected


# ── the address fence ─────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1"])
def test_private_and_loopback_addresses_are_refused(host):
    """This hub sits on a LAN with a router on it. A client id must not become
    a way to make the hub reach into that network."""
    with pytest.raises(CimdError, match="public"):
        _public_addresses(host)


def test_an_unresolvable_host_is_refused():
    with pytest.raises(CimdError):
        _public_addresses("nonexistent.invalid")


# ── reading the document ──────────────────────────────────────────────────

async def test_a_valid_document_becomes_a_client():
    client = await resolver(serving(DOCUMENT)).resolve(URL)
    assert client is not None
    assert client.client_id == URL
    assert str(client.redirect_uris[0]) == "https://client.example.com/callback"


async def test_the_client_is_public_whatever_the_document_says():
    """A document cannot confer a secret on itself: anyone who can read the URL
    could then authenticate as it."""
    client = await resolver(serving({**DOCUMENT, "client_secret": "nice-try",
                                     "token_endpoint_auth_method": "client_secret_post"})).resolve(URL)
    assert client is not None
    assert client.client_secret is None
    assert client.token_endpoint_auth_method == "none"


async def test_a_document_claiming_another_identity_is_refused():
    """Otherwise one client impersonates another by serving its id."""
    assert await resolver(serving({**DOCUMENT, "client_id": "https://other.example/doc"})).resolve(URL) is None


async def test_a_document_restating_its_own_url_is_fine():
    assert await resolver(serving({**DOCUMENT, "client_id": URL})).resolve(URL) is not None


async def test_a_document_without_redirect_uris_is_refused():
    assert await resolver(serving({"client_name": "No redirects"})).resolve(URL) is None


@pytest.mark.parametrize("payload,status", [
    (DOCUMENT, 404),
    (DOCUMENT, 500),
    ("not json at all", 200),
    ([1, 2, 3], 200),
])
async def test_unusable_responses_yield_no_client(payload, status):
    assert await resolver(serving(payload, status)).resolve(URL) is None


async def test_an_oversized_document_is_refused():
    assert await resolver(serving("x" * (MAX_BODY_BYTES + 100))).resolve(URL) is None


async def test_a_redirect_is_not_followed():
    """A public URL redirecting to a private one is the usual way past an
    address check, so redirects are refused rather than re-validated."""
    handler = serving("", 302, {"location": "http://169.254.169.254/latest/meta-data/"})
    assert await resolver(handler).resolve(URL) is None


# ── caching ───────────────────────────────────────────────────────────────

async def test_a_document_is_fetched_once():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx2.Response(200, content=json.dumps(DOCUMENT))

    r = resolver(handler)
    await r.resolve(URL)
    await r.resolve(URL)
    assert len(calls) == 1


async def test_a_failure_is_cached_too():
    """Otherwise a bad client id is one outbound request per attempt."""
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx2.Response(404)

    r = resolver(handler)
    await r.resolve(URL)
    await r.resolve(URL)
    assert len(calls) == 1


# ── the switch ────────────────────────────────────────────────────────────

async def test_disabling_it_makes_no_request():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx2.Response(200, content=json.dumps(DOCUMENT))

    assert await resolver(handler, enabled=False).resolve(URL) is None
    assert calls == []


async def test_a_registered_style_client_id_is_never_fetched():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx2.Response(200, content=json.dumps(DOCUMENT))

    assert await resolver(handler).resolve("9f8e7d6c-uuid-style") is None
    assert calls == []


# ── a URL id cannot be registered over ────────────────────────────────────

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from mcp.server.auth.provider import RegistrationError  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

from mcphub.auth.provider import HubOAuthProvider  # noqa: E402
from mcphub.db import Database  # noqa: E402


def provider() -> HubOAuthProvider:
    return HubOAuthProvider(Database(Path(tempfile.mkdtemp()) / "t.db"))


async def test_registering_a_url_client_id_is_refused():
    """Otherwise whoever registers first owns that identity, and the document
    the URL actually serves is shadowed by a stored record."""
    with pytest.raises(RegistrationError):
        await provider().register_client(OAuthClientInformationFull(
            client_id=URL, redirect_uris=[AnyUrl("https://evil.example/cb")]))


async def test_ordinary_registration_is_unaffected():
    p = provider()
    await p.register_client(OAuthClientInformationFull(
        client_id="9f8e-uuid-style", redirect_uris=[AnyUrl("https://app.example/cb")]))
    assert await p.get_client("9f8e-uuid-style") is not None


async def test_a_registered_client_wins_over_a_fetch():
    """A stored record is authoritative; nothing goes out for an id we know."""
    p = provider()
    await p.register_client(OAuthClientInformationFull(
        client_id="stored-id", redirect_uris=[AnyUrl("https://app.example/cb")]))
    assert (await p.get_client("stored-id")).client_id == "stored-id"
