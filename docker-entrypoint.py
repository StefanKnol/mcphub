#!/usr/bin/env python3
"""Container entrypoint: make the data directory usable, then drop privileges.

A bind-mounted host directory arrives owned by whoever created it — root, under
plain `docker run`, and `nobody:users` (99:100) on Unraid. A container running
as a fixed uid cannot write to it, and the failure surfaces as
`sqlite3.OperationalError: unable to open database file`, which says nothing
about ownership.

So this starts as root, aligns the data directory with PUID/PGID, and then
drops to that user before exec'ing the app — the PUID/PGID convention Unraid
users already expect. Run the container with `--user` to opt out entirely: the
non-root path below changes no ownership and only reports a directory it
cannot write.
"""

from __future__ import annotations

import os
import sys

DATA = os.environ.get("MCPHUB_DATA_DIR", "/data")
# 99:100 is nobody:users on Unraid, which owns appdata by default.
PUID = int(os.environ.get("PUID", "99"))
PGID = int(os.environ.get("PGID", "100"))


def fail(message: str) -> None:
    print(f"mcphub: {message}", file=sys.stderr)
    raise SystemExit(1)


def take_ownership() -> None:
    os.makedirs(DATA, exist_ok=True)
    os.chown(DATA, PUID, PGID)
    for root, dirs, files in os.walk(DATA):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), PUID, PGID)
            except OSError:
                # A read-only or otherwise odd mount should not stop startup;
                # if it genuinely cannot be written, the check below says so.
                pass


def drop_privileges() -> None:
    os.setgroups([])
    os.setgid(PGID)
    os.setuid(PUID)
    # An arbitrary uid has no home directory in the image.
    os.environ["HOME"] = "/tmp"


def check_writable() -> None:
    probe = os.path.join(DATA, ".write-test")
    try:
        with open(probe, "w") as fh:
            fh.write("")
        os.unlink(probe)
    except OSError as exc:
        fail(
            f"cannot write to {DATA} as uid {os.geteuid()}: {exc}\n"
            f"  The data directory holds hub.db and master.key, so this is fatal.\n"
            f"  Either let the container start as root so it can fix ownership itself,\n"
            f"  or chown the host directory to the uid you run it as:\n"
            f"      chown -R {PUID}:{PGID} <host path>"
        )


def main() -> None:
    if os.geteuid() == 0:
        take_ownership()
        drop_privileges()
    check_writable()
    os.execv(sys.executable, [sys.executable, "-m", "mcphub", *sys.argv[1:]])


if __name__ == "__main__":
    main()
