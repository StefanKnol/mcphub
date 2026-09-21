"""Letting an app use other backends on the hub, at a level someone chose.

An app that wants to call another backend has two bad options without this:
be handed a credential by hand, which nobody can then see or revoke in one
place, or be trusted to run as whoever is using it, which makes the level on
its own connector meaningless.

So an app gets an identity of its own. It is an ordinary account — username
`app:<slug>`, no password that can ever verify — which means every mechanism
this hub already has applies to it unchanged: it is granted backends at levels
in `backend_grants`, its tokens are bound to one backend each by the same
RFC 8707 resource check, and revoking it is deleting rows. Nothing about an app
grant is a second permission system standing beside the first.

Two things follow from that, and both are worth saying out loud:

- **An app's access is the app's, not the user's.** A viewer using an app that
  holds `admin` on the router reaches the router as the app. The app is told the
  person's own level in `X-Mcphub-Role` and decides what to do with it; the hub
  cannot decide for it, because over the app's own API it cannot tell an edit
  from a search.
- **Tokens live in memory.** They are minted when the app starts and kept only
  as hashes in the database, so a restart rotates them and a leaked database
  hands over nothing. An app that caches one should be ready for it to stop
  working and read its environment again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .crypto import hash_token, new_token
from .db import utcnow
from . import roles

log = logging.getLogger(__name__)

PREFIX = "app:"
"""Namespace for app accounts. A person cannot be called this: the account form
accepts no colon, so nothing a human creates can collide with one."""

NO_PASSWORD = "!"
"""A stored hash that can never verify. `verify_password` needs four `$`-joined
parts and gives up on anything else, so this fails closed rather than by luck."""

CLIENT_ID = "mcphub-app"
"""What issued the token, in the same column an OAuth client would fill. These
are not issued through an authorization flow — there is no one to consent."""

ENV_VAR = "MCPHUB_BACKENDS"
URL_VAR = "MCPHUB_URL"
HEADER = "x-mcphub-backends"


def account_name(slug: str) -> str:
    return f"{PREFIX}{slug}"


def is_app(username: str | None) -> bool:
    return bool(username) and str(username).startswith(PREFIX)


class AppAccess:
    """The app identities on one hub, and the credentials they are holding."""

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        # slug -> {target slug: {"url", "token", "level"}}. The plaintext lives
        # here and nowhere else; the database has only hashes.
        self._issued: dict[str, dict[str, dict[str, str]]] = {}

    # ── grants ────────────────────────────────────────────────────────────

    def grants(self, slug: str) -> dict[str, str]:
        """Which backends this app may use, and at which level."""
        return {r["slug"]: roles.normalise(r["role"]) for r in self._hub.db.query(
            "SELECT b.slug, g.role FROM backend_grants g "
            "JOIN backends b ON b.id = g.backend_id "
            "JOIN users u ON u.id = g.user_id WHERE u.username = ?",
            (account_name(slug),))}

    def set_grants(self, slug: str, wanted: dict[str, str]) -> None:
        """Replace what this app may reach. An empty mapping removes its account.

        Removing rather than leaving an account with no grants: an identity that
        can reach nothing is only a row for someone to wonder about later.
        """
        wanted = {target: roles.normalise(level) for target, level in wanted.items()
                  if target != slug}
        self.revoke(slug)
        if not wanted:
            self.forget(slug)
            return
        user_id = self._ensure_account(slug)
        self._hub.db.execute("DELETE FROM backend_grants WHERE user_id = ?", (user_id,))
        for target, level in wanted.items():
            row = self._hub.db.one("SELECT id FROM backends WHERE slug = ?", (target,))
            if row is None:
                continue
            self._hub.db.execute(
                "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
                "VALUES (?, ?, ?, ?)", (user_id, row["id"], level, utcnow()))
        log.info("app %s may now use %s", slug, sorted(wanted.items()))

    def _ensure_account(self, slug: str) -> int:
        name = account_name(slug)
        row = self._hub.db.one("SELECT id FROM users WHERE username = ?", (name,))
        if row is not None:
            return int(row["id"])
        self._hub.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, "
            "created_at) VALUES (?, ?, 0, 0, ?)", (name, NO_PASSWORD, utcnow()))
        return int(self._hub.db.one("SELECT id FROM users WHERE username = ?",
                                    (name,))["id"])

    # ── credentials ───────────────────────────────────────────────────────

    def issue(self, slug: str) -> dict[str, dict[str, str]]:
        """This app's credentials, minted on first use and kept for the process.

        One token per backend it was granted, because a token here is bound to
        one endpoint — the same rule that stops a token for the router being
        replayed against the Unraid one applies to an app's tokens too.
        """
        held = self._issued.get(slug)
        if held is not None:
            return held

        granted = self.grants(slug)
        if not granted:
            self._issued[slug] = {}
            return {}

        user_id = self._ensure_account(slug)
        self._clear_tokens(user_id)
        base = self._hub.settings.public_url.rstrip("/")
        minted: dict[str, dict[str, str]] = {}
        for target, level in granted.items():
            token = new_token()
            resource = f"{base}/mcp/{target}"
            self._hub.db.execute(
                "INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, "
                "resource, expires_at, created_at) VALUES (?, 'access', ?, ?, ?, ?, NULL, ?)",
                (hash_token(token), CLIENT_ID, user_id, "mcp:use", resource, utcnow()))
            minted[target] = {"url": resource, "token": token, "level": level}
        self._issued[slug] = minted
        log.info("app %s issued credentials for %s", slug, sorted(minted))
        return minted

    def environment(self, slug: str) -> dict[str, str]:
        """What a launched app finds in its environment. Empty if it has no grants."""
        held = self.issue(slug)
        if not held:
            return {}
        return {URL_VAR: self._hub.settings.public_url.rstrip("/"),
                ENV_VAR: json.dumps(held, separators=(",", ":"))}

    def header(self, slug: str) -> dict[str, str]:
        """The same, for an app the hub talks to over HTTP rather than starts."""
        held = self.issue(slug)
        return {HEADER: json.dumps(held, separators=(",", ":"))} if held else {}

    # ── taking it back ────────────────────────────────────────────────────

    def revoke(self, slug: str) -> None:
        """Drop this app's live credentials. It gets fresh ones when it restarts."""
        self._issued.pop(slug, None)
        row = self._hub.db.one("SELECT id FROM users WHERE username = ?",
                               (account_name(slug),))
        if row is not None:
            self._clear_tokens(int(row["id"]))

    def forget(self, slug: str) -> None:
        """Remove the app's identity entirely, for a backend that is going away."""
        self.revoke(slug)
        self._hub.db.execute("DELETE FROM users WHERE username = ?", (account_name(slug),))

    def rename(self, old: str, new: str) -> None:
        if old == new:
            return
        self.revoke(old)
        self._hub.db.execute("UPDATE users SET username = ? WHERE username = ?",
                             (account_name(new), account_name(old)))

    def _clear_tokens(self, user_id: int) -> None:
        self._hub.db.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))


def environment_for(instance: Any) -> dict[str, str]:
    """The credential variables for a started backend, from what it was handed.

    Reads `BackendInstance.granted` rather than the hub, so a plugin building a
    server needs no more than the instance it was given.
    """
    granted = getattr(instance, "granted", None) or {}
    if not granted:
        return {}
    base = next(iter(granted.values()))["url"].rsplit("/mcp/", 1)[0]
    return {URL_VAR: base, ENV_VAR: json.dumps(granted, separators=(",", ":"))}


def header_for(instance: Any) -> dict[str, str]:
    granted = getattr(instance, "granted", None) or {}
    return {HEADER: json.dumps(granted, separators=(",", ":"))} if granted else {}
