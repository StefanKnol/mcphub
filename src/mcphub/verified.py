"""Which registry servers have actually been run against this hub.

The registry lists what people have published; it says nothing about whether a
given server works once mcphub launches it. This is the difference between "the
author says it is an MCP server" and "we started it and it answered".

Entries are produced by `scripts/verify_servers.py`. Nothing here is a claim by
the server's author, which is the whole point — a self-declared compatibility
flag would be exactly the hope-it-works badge this exists to replace.

Two levels, because they are worth very different amounts:

- **launched** — the server started, completed a handshake and listed its
  tools. That is a liveness check and nothing more. The MikroTik server this
  project was built to replace would pass it comfortably: it starts fine and
  lists 182 tools fine, and every one of its write tools is broken.
- **verified** — that, plus every behavioural probe the entry declares actually
  ran and returned what it should. A probe is a read-only tool call with an
  expected outcome, so it exercises the server's logic rather than its
  existence.

Neither level says every tool works, or that a server is safe to run. A probe
says the behaviour it names is the behaviour observed.
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
    probes_passed: int = 0

    @property
    def ok(self) -> bool:
        """Launched at all. The weaker of the two levels."""
        return self.status in {"ok", "launched"}

    @property
    def probed(self) -> bool:
        """Behaviour was actually exercised, not merely listed."""
        return self.status == "ok" and self.probes_passed > 0

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
        if self.probed:
            checks = "check" if self.probes_passed == 1 else "checks"
            return f"Verified — {tools}, {self.probes_passed} {checks}"
        # Deliberately not "Verified": nothing about this server's behaviour
        # was tested, only that it answers.
        return f"Launches — {tools}"


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
            probes_passed=int(row.get("probesPassed") or 0),
        )
    return entries


def lookup(name: str) -> Verification | None:
    return _load().get(name)


def all_verified() -> list[Verification]:
    return [v for v in _load().values() if v.ok]
