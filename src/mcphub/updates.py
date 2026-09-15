"""Background check for newer versions of registry-backed backends.

The registry serves repeated queries from its own cache (`X-Registry-Cache:
HIT`), publishes no rate limit, and each check is one small query per backend.
Hourly is therefore unremarkable traffic and still surfaces a release the same
day. The interval is configurable for anyone who disagrees.

What this does *not* do is update anything. It records that a newer version
exists; moving to it stays a deliberate press of Update, or a pin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

from . import registry as mcp_registry

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 3600
MIN_INTERVAL = 300
"""A floor, so a misconfigured interval cannot turn this into a hot loop
against someone else's service."""

LATEST_KEY = "latest_version"
CHECKED_KEY = "latest_checked_at"


class UpdateChecker:
    """Polls the registry for newer versions, and records what it finds."""

    def __init__(self, hub: Any, interval: int = DEFAULT_INTERVAL) -> None:
        self._hub = hub
        self._interval = max(MIN_INTERVAL, interval)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._failures = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="update-checker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
            self._task = None

    async def _run(self) -> None:
        # Staggered, so a fleet of hubs restarted together does not arrive at
        # the registry in lockstep.
        await self._sleep(random.uniform(30, 120))
        while not self._stop.is_set():
            try:
                checked = await self.check_all()
                self._failures = 0
                if checked:
                    log.info("update check: %d backend(s) checked", checked)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed check must not end the loop
                self._failures += 1
                log.warning("update check failed (%d in a row)", self._failures, exc_info=True)

            # Back off after repeated failures rather than continuing at full
            # rate against something that is evidently unwell.
            delay = self._interval * min(2**self._failures, 8)
            await self._sleep(delay * random.uniform(0.9, 1.1))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def check_all(self) -> int:
        """Check every registry-backed backend. Returns how many were checked."""
        checked = 0
        seen: dict[str, str] = {}
        for row in self._hub.backend_rows():
            try:
                config = json.loads(row["config_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            name = config.get("registry_name")
            if not name:
                continue

            if name in seen:
                latest = seen[name]
            else:
                try:
                    available = await mcp_registry.versions(name)
                except mcp_registry.RegistryError as exc:
                    log.info("could not check %s: %s", name, exc)
                    continue
                latest = next((v.version for v in available if v.is_latest),
                              available[0].version if available else "")
                seen[name] = latest
            checked += 1

            if not latest or config.get(LATEST_KEY) == latest:
                self._touch(row, config, latest)
                continue
            log.info("backend %s: registry has %s, running %s",
                     row["slug"], latest, config.get("upstream_version") or "?")
            self._touch(row, config, latest)
        return checked

    def _touch(self, row: Any, config: dict[str, Any], latest: str) -> None:
        config[LATEST_KEY] = latest
        config[CHECKED_KEY] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._hub.db.execute("UPDATE backends SET config_json = ? WHERE id = ?",
                             (json.dumps(config), row["id"]))
