"""Authorization server behaviour that the end-to-end flow depends on."""

import tempfile
import time
from pathlib import Path

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcphub.auth.provider import SCOPE_USE, HubOAuthProvider
from mcphub.crypto import hash_password, hash_token
from mcphub.db import Database, utcnow

REDIRECT = "http://localhost:9999/callback"


@pytest.fixture
def provider():
    db = Database(Path(tempfile.mkdtemp()) / "t.db")
    db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
               ("admin", hash_password("hunter2hunter2"), utcnow()))
    return HubOAuthProvider(db), db


def client(client_id="c1"):
    return OAuthClientInformationFull(
        client_id=client_id, client_secret="s3cret",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
    )


def params(resource=None, state="st"):
    return AuthorizationParams(
        state=state, scopes=[SCOPE_USE], code_challenge="chal",
        redirect_uri=AnyUrl(REDIRECT), redirect_uri_provided_explicitly=True, resource=resource,
    )


async def test_password_check(provider):
    p, _ = provider
    assert p.authenticate_user("admin", "hunter2hunter2") is not None
    assert p.authenticate_user("admin", "wrong") is None
    assert p.authenticate_user("nobody", "hunter2hunter2") is None


async def test_authorize_requires_pkce(provider):
    p, _ = provider
    await p.register_client(client())
    bad = params()
    bad.code_challenge = ""
    with pytest.raises(Exception):
        await p.authorize(client(), bad)


async def test_full_code_exchange_binds_resource_and_subject(provider):
    p, _ = provider
    await p.register_client(client())
    resource = "https://hub.example/mcp/router"
    url = await p.authorize(client(), params(resource=resource))
    req_id = url.split("req=")[1]

    user_id = p.authenticate_user("admin", "hunter2hunter2")
    redirect = p.complete_authorization(req_id, user_id)
    code = redirect.split("code=")[1].split("&")[0]

    loaded = await p.load_authorization_code(client(), code)
    assert loaded is not None
    assert loaded.resource == resource
    assert loaded.subject == "admin"

    token = await p.exchange_authorization_code(client(), loaded)
    access = await p.load_access_token(token.access_token)
    assert access is not None
    assert access.resource == resource, "the token must stay bound to the backend it was issued for"
    assert access.subject == "admin"


async def test_authorization_code_is_single_use(provider):
    p, _ = provider
    await p.register_client(client())
    url = await p.authorize(client(), params())
    user_id = p.authenticate_user("admin", "hunter2hunter2")
    code = p.complete_authorization(url.split("req=")[1], user_id).split("code=")[1].split("&")[0]
    loaded = await p.load_authorization_code(client(), code)

    await p.exchange_authorization_code(client(), loaded)
    with pytest.raises(TokenError):
        await p.exchange_authorization_code(client(), loaded)


async def test_a_client_cannot_redeem_another_clients_code(provider):
    p, _ = provider
    await p.register_client(client("c1"))
    await p.register_client(client("c2"))
    url = await p.authorize(client("c1"), params())
    user_id = p.authenticate_user("admin", "hunter2hunter2")
    code = p.complete_authorization(url.split("req=")[1], user_id).split("code=")[1].split("&")[0]

    assert await p.load_authorization_code(client("c2"), code) is None


async def test_refresh_rotates_and_cannot_widen_scope(provider):
    p, _ = provider
    await p.register_client(client())
    url = await p.authorize(client(), params())
    user_id = p.authenticate_user("admin", "hunter2hunter2")
    code = p.complete_authorization(url.split("req=")[1], user_id).split("code=")[1].split("&")[0]
    token = await p.exchange_authorization_code(client(), await p.load_authorization_code(client(), code))

    rt = await p.load_refresh_token(client(), token.refresh_token)
    fresh = await p.exchange_refresh_token(client(), rt, [SCOPE_USE])
    assert fresh.access_token != token.access_token
    assert await p.load_refresh_token(client(), token.refresh_token) is None, "old refresh token must die"

    rt2 = await p.load_refresh_token(client(), fresh.refresh_token)
    with pytest.raises(TokenError):
        await p.exchange_refresh_token(client(), rt2, [SCOPE_USE, "admin:everything"])


async def test_tokens_are_stored_hashed(provider):
    p, db = provider
    await p.register_client(client())
    url = await p.authorize(client(), params())
    user_id = p.authenticate_user("admin", "hunter2hunter2")
    code = p.complete_authorization(url.split("req=")[1], user_id).split("code=")[1].split("&")[0]
    token = await p.exchange_authorization_code(client(), await p.load_authorization_code(client(), code))

    stored = [r["token_hash"] for r in db.query("SELECT token_hash FROM tokens")]
    assert token.access_token not in stored, "a database leak must not hand over live tokens"
    assert hash_token(token.access_token) in stored


async def test_expired_access_token_is_rejected_and_purged(provider):
    p, db = provider
    await p.register_client(client())
    url = await p.authorize(client(), params())
    user_id = p.authenticate_user("admin", "hunter2hunter2")
    code = p.complete_authorization(url.split("req=")[1], user_id).split("code=")[1].split("&")[0]
    token = await p.exchange_authorization_code(client(), await p.load_authorization_code(client(), code))

    db.execute("UPDATE tokens SET expires_at = ? WHERE kind = 'access'", (time.time() - 1,))
    assert await p.load_access_token(token.access_token) is None
    assert db.query("SELECT 1 FROM tokens WHERE kind = 'access'") == []


async def test_expired_pending_authorization_is_not_completable(provider):
    p, _ = provider
    await p.register_client(client())
    url = await p.authorize(client(), params())
    req_id = url.split("req=")[1]
    p._pending[req_id].expires_at = time.time() - 1

    assert p.get_pending(req_id) is None
    with pytest.raises(Exception):
        p.complete_authorization(req_id, 1)
