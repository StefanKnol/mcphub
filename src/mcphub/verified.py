"""Which registry servers have actually been run against this hub.

The registry lists what people have published; it says nothing about whether a
given server works once mcphub launches it. This is the difference between "the
author says it is an MCP server" and "we started it and it answered".

Entries are produced by `scripts/verify_servers.py`, which launches each server
and records the tool count it observed. Nothing here is a claim by the server's
author, which is the whole point — a self-declared compatibility flag would be
exactly the hope-it-works badge this exists to replace.

What a verified entry does *not* mean: that every tool works, or that the server
is safe. It means it launched, completed an MCP handshake, and listed its tools.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "verified.json"
STALE_AFTER_DAYS = 90


@dataclass(frozen=True)
class Verification:
    name: str
    note: str = ""
    status: str = "unchecked"
    tool_count: int | None = None
    last_verified: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def stale(self) -> bool:
        """True once a check is old enough that it no longer says much."""
        if not self.last_verified:
            return True
        try:
            checked = datetime.fromisoformat(self.last_verified).date()
        except ValueError:
            return True
        return (date.today() - checked).days > STALE_AFTER_DAYS

    @property
    def summary(self) -> str:
        if not self.ok:
            return "Not verified"
        tools = f"{self.tool_count} tools" if self.tool_count else "launched"
        return f"Verified — {tools}"


@lru_cache(maxsize=1)
def _load() -> dict[str, Verification]:
    try:
        payload = json.loads(DATA_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("verified.json could not be read; no servers will show as verified")
        return {}

    entries: dict[str, Verification] = {}
    for row in payload.get("servers") or []:
        name = row.get("name")
        if not name:
            continue
        entries[name] = Verification(
            name=name,
            note=row.get("note", ""),
            status=row.get("status", "unchecked"),
            tool_count=row.get("toolCount"),
            last_verified=row.get("lastVerified"),
        )
    return entries


def lookup(name: str) -> Verification | None:
    return _load().get(name)


def all_verified() -> list[Verification]:
    return [v for v in _load().values() if v.ok]
