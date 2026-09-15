"""Who may reach which backend.

Backends are shared, so the grant is what stands between an account and
someone else's router. The check runs per request rather than at token issue,
which is what makes revoking a grant take effect at once instead of whenever
the token happens to expire.
"""

import tempfile
from pathlib import Path

import pytest

from mcphub.crypto import hash_password
from mcphub.db import Database, utcnow
from mcphub.mounts import _Authorized
from mcphub.web.routes import _resource_slug


@pytest.fixture
def db():
    database = Database(Path(tempfile.mkdtemp()) / "t.db")
    for name, admin in (("admin", 1), ("colleague", 0), ("stranger", 0)):
        database.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, ?, 0, ?)", (name, hash_password("x" * 12), admin, utcnow()))
    for slug in ("alpha", "beta"):
        database.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, updated_at) "
            "VALUES (?, 'mcp-proxy', ?, 1, '{}', ?, ?)", (slug, slug, utcnow(), utcnow()))
    database.execute(
        "INSERT INTO backend_grants (user_id, backend_id, created_at) "
        "SELECT u.id, b.id, ? FROM users u, backends b WHERE u.username='colleague' AND b.slug='alpha'",
        (utcnow(),))
    return database


def guard(db, slug):
    return _Authorized(app=None, db=db, slug=slug)


def test_a_grant_permits(db):
    assert guard(db, "alpha")._permitted("colleague")


def test_no_grant_refuses(db):
    assert not guard(db, "beta")._permitted("colleague")


def test_an_account_with_no_grants_reaches_nothing(db):
    assert not guard(db, "alpha")._permitted("stranger")


def test_an_admin_needs_no_grant(db):
    """Writing admin access as data would let one bad row lock everyone out."""
    assert guard(db, "alpha")._permitted("admin")
    assert guard(db, "beta")._permitted("admin")


def test_revoking_takes_effect_without_reissuing_anything(db):
    assert guard(db, "alpha")._permitted("colleague")
    db.execute("DELETE FROM backend_grants WHERE user_id = "
               "(SELECT id FROM users WHERE username = 'colleague')")
    assert not guard(db, "alpha")._permitted("colleague"), (
        "the check must run per request, or a revoked account keeps working "
        "until its token happens to expire"
    )


def test_a_deleted_account_is_refused(db):
    db.execute("DELETE FROM users WHERE username = 'colleague'")
    assert not guard(db, "alpha")._permitted("colleague")


@pytest.mark.parametrize("subject", [None, "", "nobody"])
def test_an_unknown_subject_is_refused(db, subject):
    assert not guard(db, "alpha")._permitted(subject)


def test_a_grant_is_for_one_backend_not_all(db):
    """A grant row names a backend; it must not read as a blanket permission."""
    assert guard(db, "alpha")._permitted("colleague")
    assert not guard(db, "beta")._permitted("colleague")


# ── which backend a token was requested for ───────────────────────────────

@pytest.mark.parametrize("resource,expected", [
    ("https://h/mcp/router", "router"),
    ("https://h/mcp/router/", "router"),
    ("https://h/base/mcp/a-b", "a-b"),
    ("https://h/elsewhere", None),
    (None, None),
    ("", None),
])
def test_resource_slug(resource, expected):
    assert _resource_slug(resource) == expected
