#!/usr/bin/env python
"""Launch each listed server and record what it actually did.

Run from CI on a schedule. The output is the only thing that entitles a server
to a "Verified" badge in the registry browser, which is the point: a badge that
came from an author's own claim would carry no information.

    uv run python scripts/verify_servers.py [--write]

Without --write it reports and exits non-zero on any regression, so a scheduled
run fails loudly when a server that used to work stops working.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcphub import registry as mcp_registry  # noqa: E402
from mcphub.plugins.builtin.mcpproxy.upstream import Upstream, UpstreamConfig  # noqa: E402

DATA_FILE = Path(__file__).resolve().parents[1] / "src" / "mcphub" / "data" / "verified.json"
LAUNCH_TIMEOUT = 180

# Run before anything else, so "every server failed" is distinguishable from
# "the harness is broken". A published server with no configuration to get
# wrong: if this cannot be launched, nothing below means anything.
SELF_TEST = "uvx mcp-server-time"


async def resolve_command(entry: dict) -> str:
    """The command to launch, from the entry or from the registry."""
    if entry.get("command"):
        return str(entry["command"])
    server = await mcp_registry.get(entry["name"])
    if server is None:
        raise LookupError(f"{entry['name']} is not in the registry")
    if not server.command:
        raise LookupError(f"{entry['name']} is remote-only; nothing to launch")
    return server.command


async def verify(entry: dict) -> dict:
    """Return the entry with observed fields replaced by this run's findings."""
    result = dict(entry)
    try:
        command = await resolve_command(entry)
    except LookupError as exc:
        return {**result, "status": "unavailable", "toolCount": None,
                "lastVerified": date.today().isoformat(), "detail": str(exc)}

    upstream = Upstream(UpstreamConfig(
        command=command,
        # Dummy values so a server that validates its configuration at startup
        # still starts. Nothing here reaches a real device: 192.0.2.1 is
        # TEST-NET-1 and is guaranteed not to route.
        env={str(k): str(v) for k, v in (entry.get("verifyEnv") or {}).items()},
        timeout=LAUNCH_TIMEOUT,
    ))
    try:
        tools = await asyncio.wait_for(upstream.list_tools(), timeout=LAUNCH_TIMEOUT)
        info = await upstream.server_info()
        return {**result, "status": "ok", "toolCount": len(tools),
                "lastVerified": date.today().isoformat(),
                "detail": f"{info.get('name') or '?'} {info.get('version') or ''}".strip(),
                "command": command}
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        return {**result, "status": "failed", "toolCount": None,
                "lastVerified": date.today().isoformat(),
                "detail": str(exc).splitlines()[0][:200]}
    finally:
        await upstream.close()


async def self_test() -> bool:
    upstream = Upstream(UpstreamConfig(command=SELF_TEST, timeout=LAUNCH_TIMEOUT))
    try:
        tools = await asyncio.wait_for(upstream.list_tools(), timeout=LAUNCH_TIMEOUT)
        print(f"  harness  {SELF_TEST} -> {len(tools)} tools\n")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  HARNESS BROKEN: {SELF_TEST} -> {str(exc).splitlines()[0][:160]}\n", file=sys.stderr)
        return False
    finally:
        await upstream.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist the results to verified.json")
    args = parser.parse_args()

    if not await self_test():
        return 2

    payload = json.loads(DATA_FILE.read_text())
    entries = payload.get("servers") or []

    results, regressed = [], []
    for entry in entries:
        was_ok = entry.get("status") == "ok"
        outcome = await verify(entry)
        results.append(outcome)

        mark = {"ok": "ok      ", "failed": "FAILED  ", "unavailable": "n/a     "}.get(outcome["status"], "?       ")
        tools = f"{outcome['toolCount']} tools" if outcome.get("toolCount") else ""
        print(f"  {mark} {outcome['name']:52} {tools:10} {outcome.get('detail', '')[:60]}")

        if was_ok and outcome["status"] != "ok":
            regressed.append(outcome["name"])

    if args.write:
        payload["servers"] = results
        DATA_FILE.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwrote {DATA_FILE.relative_to(Path.cwd())}")

    verified = sum(1 for r in results if r["status"] == "ok")
    print(f"\n{verified}/{len(results)} verified")
    if regressed:
        print("REGRESSED: " + ", ".join(regressed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
