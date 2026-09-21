"""SQLite storage.

Everything mutable lives here: backend instances, their credentials, OAuth
client registrations, and issued tokens. One file, one volume to back up.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    username         TEXT NOT NULL UNIQUE,
    password_hash    TEXT NOT NULL,
    is_admin         INTEGER NOT NULL DEFAULT 0,
    can_add_backends INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

-- Which accounts may reach which backends, and how far. Backends are shared:
-- configured once, granted out. An admin needs no row here — they reach
-- everything, and writing that as data would let a mistake lock everyone out
-- of their own hub.
--
-- `role` is one of mcphub.roles.LEVELS. It is not a second grant: the row says
-- whether the account may reach the backend at all, the role says which of its
-- tools it gets once it is there.
CREATE TABLE IF NOT EXISTS backend_grants (
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    backend_id INTEGER NOT NULL REFERENCES backends (id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, backend_id)
);

-- One row per *backend instance*, not per plugin: two MikroTik routers wrapped
-- through the proxy are two rows sharing plugin_id='mcp-proxy'. Each row is
-- mounted at /mcp/{slug} and is registered in Claude as its own connector, so
-- tool surfaces never merge.
CREATE TABLE IF NOT EXISTS backends (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    plugin_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    config_json  TEXT NOT NULL DEFAULT '{}',
    secrets_blob BLOB,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- A version an account chose to stay on. Not a permission: anyone granted a
-- backend may pin it, and the pin only affects what *they* get. Two accounts
-- on different versions means the hub runs both, which is the cost of letting
-- someone hold back while someone else moves on.
CREATE TABLE IF NOT EXISTS backend_pins (
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    backend_id INTEGER NOT NULL REFERENCES backends (id) ON DELETE CASCADE,
    version    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, backend_id)
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    secret_hash   TEXT,
    metadata_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code           TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    user_id        INTEGER NOT NULL,
    redirect_uri   TEXT NOT NULL,
    explicit_uri   INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes         TEXT NOT NULL,
    resource       TEXT,
    expires_at     REAL NOT NULL
);

-- Access and refresh tokens, stored as SHA-256 hashes (see crypto.hash_token).
-- `resource` is the RFC 8707 indicator: it pins a token to one backend
-- endpoint, so a token minted for the router connector is rejected outright
-- if it is replayed against the Unraid one.
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id  TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    scopes     TEXT NOT NULL,
    resource   TEXT,
    expires_at REAL,
    created_at TEXT NOT NULL
);

-- Browser sessions for the config UI. Opaque random tokens in a table rather
-- than signed cookies: revoking one is a DELETE, and no signing key has to
-- stay consistent across restarts.
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens (client_id);
CREATE INDEX IF NOT EXISTS idx_backends_slug ON backends (slug);
CREATE INDEX IF NOT EXISTS idx_grants_user ON backend_grants (user_id);
CREATE INDEX IF NOT EXISTS idx_pins_backend ON backend_pins (backend_id);
"""

# `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so columns added
# later never appear on a database that predates them. Each entry runs only if
# its column is missing.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("users", "is_admin", "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"),
    ("users", "can_add_backends",
     "ALTER TABLE users ADD COLUMN can_add_backends INTEGER NOT NULL DEFAULT 0"),
    # Grants that predate levels keep the access they already had, which is
    # 'user' — everything the backend offers short of what it marks destructive.
    # Defaulting them to 'viewer' would silently take away tools people were
    # using; to 'admin' would silently hand out ones they never had.
    ("backend_grants", "role",
     "ALTER TABLE backend_grants ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin synchronous SQLite wrapper, called from async code via `run`.

    A single connection guarded by a lock is plenty for a self-hosted hub, and
    it sidesteps the write-concurrency surprises of pooling under SQLite.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        for table, column, statement in MIGRATIONS:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(statement)

        # A hub that predates accounts has exactly one user, who has been
        # administering it all along. Leaving them non-admin would lock them
        # out of the settings they already owned.
        row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        if row and row[0] == 1:
            self._conn.execute(
                "UPDATE users SET is_admin = 1, can_add_backends = 1 WHERE is_admin = 0"
            )

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid or 0

    async def run(self, fn, *args):  # type: ignore[no-untyped-def]
        """Run a blocking DB callable off the event loop."""
        return await anyio.to_thread.run_sync(fn, *args)

    def purge_expired(self) -> int:
        """Drop expired codes and tokens. Cheap; called opportunistically."""
        now = time.time()
        with self.cursor() as cur:
            cur.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
            removed = cur.rowcount
            cur.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
            removed += cur.rowcount
            cur.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
            return removed + cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
