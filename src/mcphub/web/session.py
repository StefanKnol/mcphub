"""Browser sessions for the config UI."""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response

from ..crypto import hash_token, new_token
from ..db import Database, utcnow

COOKIE_NAME = "mcphub_session"
SESSION_TTL = 12 * 3600
REMEMBERED_TTL = 30 * 86400
"""A "stay signed in" session. Still a real session row, so signing out or
deleting the account ends it at once — a long-lived cookie that outlives its
record would be the thing worth avoiding."""


def start_session(db: Database, response: Response, user_id: int, *,
                  secure: bool, remember: bool = False) -> None:
    token = new_token()
    ttl = REMEMBERED_TTL if remember else SESSION_TTL
    db.execute(
        "INSERT INTO web_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), user_id, time.time() + ttl, utcnow()),
    )
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=ttl,
        httponly=True,
        # Lax rather than Strict: the OAuth flow returns to this origin via a
        # cross-site redirect, and Strict would drop the cookie on the way back
        # and bounce a just-authenticated user to the login page.
        samesite="lax",
        secure=secure,
        path="/",
    )


def end_session(db: Database, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (hash_token(token),))
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(db: Database, request: Request) -> dict[str, object] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.one(
        "SELECT s.user_id, s.expires_at, u.username FROM web_sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (hash_token(token),),
    )
    if row is None:
        return None
    if row["expires_at"] < time.time():
        db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (hash_token(token),))
        return None
    return {"id": int(row["user_id"]), "username": row["username"]}
