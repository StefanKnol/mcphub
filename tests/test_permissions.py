"""Who may reach which backend, and how far.

Backends are shared, so the grant is what stands between an account and
someone else's router. The check runs per request rather than at token issue,
which is what makes revoking a grant take effect at once instead of whenever
the token happens to expire.

The same query answers both halves: `_level` returns None for an account that
may not reach the backend at all, and otherwise the level it holds there.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from mcphub import roles
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
        "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
        "SELECT u.id, b.id, 'user', ? FROM users u, backends b "
        "WHERE u.username='colleague' AND b.slug='alpha'",
        (utcnow(),))
    return database


def guard(db, slug):
    return _Authorized(app=None, db=db, slug=slug)


def test_a_grant_permits(db):
    assert guard(db, "alpha")._level("colleague") == roles.USER


def test_no_grant_refuses(db):
    assert None is guard(db, "beta")._level("colleague")


def test_an_account_with_no_grants_reaches_nothing(db):
    assert None is guard(db, "alpha")._level("stranger")


def test_an_admin_needs_no_grant(db):
    """Writing admin access as data would let one bad row lock everyone out."""
    assert guard(db, "alpha")._level("admin") == roles.ADMIN
    assert guard(db, "beta")._level("admin") == roles.ADMIN


def test_revoking_takes_effect_without_reissuing_anything(db):
    assert guard(db, "alpha")._level("colleague") == roles.USER
    db.execute("DELETE FROM backend_grants WHERE user_id = "
               "(SELECT id FROM users WHERE username = 'colleague')")
    assert guard(db, "alpha")._level("colleague") is None, (
        "the check must run per request, or a revoked account keeps working "
        "until its token happens to expire"
    )


def test_a_deleted_account_is_refused(db):
    db.execute("DELETE FROM users WHERE username = 'colleague'")
    assert None is guard(db, "alpha")._level("colleague")


@pytest.mark.parametrize("subject", [None, "", "nobody"])
def test_an_unknown_subject_is_refused(db, subject):
    assert None is guard(db, "alpha")._level(subject)


def test_a_grant_is_for_one_backend_not_all(db):
    """A grant row names a backend; it must not read as a blanket permission."""
    assert guard(db, "alpha")._level("colleague") == roles.USER
    assert guard(db, "beta")._level("colleague") is None


# ── how far a grant goes ──────────────────────────────────────────────────

@pytest.mark.parametrize("stored,expected",
                         [("viewer", roles.VIEWER), ("user", roles.USER),
                          ("admin", roles.ADMIN),
                          # Anything the database should never hold reads as the
                          # middle level rather than as the widest one.
                          ("root", roles.USER), ("", roles.USER)])
def test_the_stored_level_is_what_the_account_gets(db, stored, expected):
    db.execute("UPDATE backend_grants SET role = ?", (stored,))
    assert guard(db, "alpha")._level("colleague") == expected


def test_a_level_does_not_reach_a_backend_without_a_grant(db):
    """`admin` on one backend must not read as admin on another."""
    db.execute("UPDATE backend_grants SET role = 'admin'")
    assert guard(db, "alpha")._level("colleague") == roles.ADMIN
    assert guard(db, "beta")._level("colleague") is None


def test_grants_made_before_levels_existed_keep_the_access_they_had():
    """The column was added later, to tables that already held grants.

    `CREATE TABLE IF NOT EXISTS` leaves those alone, so the ALTER is the only
    thing that reaches them — and what it defaults to is what every existing
    grant silently becomes. `viewer` would quietly take away tools people were
    already using; `admin` would quietly hand out ones they never had.
    """
    path = Path(tempfile.mkdtemp()) / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                            password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE backends (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
                               plugin_id TEXT NOT NULL, title TEXT NOT NULL,
                               enabled INTEGER NOT NULL DEFAULT 1,
                               config_json TEXT NOT NULL DEFAULT '{}',
                               created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE backend_grants (user_id INTEGER NOT NULL, backend_id INTEGER NOT NULL,
                                     created_at TEXT NOT NULL, PRIMARY KEY (user_id, backend_id));
        INSERT INTO users VALUES (1, 'colleague', 'x', '2024-01-01');
        INSERT INTO users VALUES (2, 'other', 'x', '2024-01-01');
        INSERT INTO backends VALUES (1, 'alpha', 'mcp-proxy', 'Alpha', 1, '{}', '2024-01-01', '2024-01-01');
        INSERT INTO backend_grants VALUES (1, 1, '2024-01-01');
    """)
    old.commit()
    old.close()

    assert guard(Database(path), "alpha")._level("colleague") == roles.USER


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
