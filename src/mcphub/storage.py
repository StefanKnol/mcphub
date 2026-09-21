"""Somewhere a backend can keep things between restarts.

Every backend gets a directory of its own under the data volume, named after
its slug. A database, an index, whatever it needs — the hub does not care what
goes in, only that it survives a container restart and is backed up with
everything else.

There is an honest limit to this, and it is worth stating plainly rather than
discovering later. The hub can *provide* the directory only to a server it
launches itself, because such a server is its own subprocess and shares its
filesystem. A backend reached over a URL — an app in another container, or on
another machine — is told the path and nothing more; whether anything is there
is between that container and whoever wrote its compose file. The hub creates
the directory either way, so the path exists to be mounted, but it cannot mount
it on something else's behalf.

The alternative — a storage API the hub serves and apps call back into — would
work across that boundary, but only for an app written against it. This works
for anything that can be told where to put its files, which is nearly
everything, so it is the default.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

ENV_VAR = "MCPHUB_STORAGE"
"""What a launched server finds the path in."""

DIRNAME = "apps"

ENABLED = "storage_enabled"
"""Config key asking the hub for a directory for this backend.

Off by default. A plugin answers `uses_storage()` from whatever it likes; the
generic proxy answers it from this, because whether a wrapped server writes
anything is not something the hub can work out on its own — only the person who
chose that server knows."""

CUSTOM_VAR = "storage_var"
"""Config key naming a second environment variable to set to the same path.

Almost no server asks for its data directory as MCPHUB_STORAGE; it wants
DB_PATH or STATE_DIR or whatever it chose. Writing `DB_PATH=$MCPHUB_STORAGE`
into the environment box works and is what a plugin author would do, but for
someone wiring up an npm package it is a piece of syntax to get right for no
reason. Typing the variable's name is the same instruction without the syntax.
"""

PER_VERSION = "storage_per_version"
"""Config key asking for a directory per version rather than one shared.

Off by default. The case that brought storage about is several accounts working
on the same data while sitting on different versions of the server that serves
it — a dictionary two people are editing while one of them tries a new release.
Sharing is what makes that work, so sharing is the default.

Turn it on for a backend whose versions keep something they cannot share, such
as a derived index whose format changed. It does not make two versions safe to
run against one dataset: they would still share any schema migration either of
them applies, and nothing here can undo that."""

_SAFE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,62}$")
_REFERENCE = re.compile(r"\$\{" + ENV_VAR + r"\}|\$" + ENV_VAR + r"(?![A-Za-z0-9_])")


def path_for(data_dir: Path, slug: str, version: str = "") -> Path:
    """Where this backend's own files live. Does not create anything.

    One directory per backend, shared by every version of it and every account
    using it — which is the point when the data is the thing people are working
    on together. `version` asks for a directory of its own instead, for a
    backend whose versions keep something they cannot share.
    """
    if not _SAFE.match(slug):
        # Slugs are already narrower than this, but the path is built from one,
        # and a rule that only holds somewhere else is not a rule.
        raise ValueError(f"{slug!r} cannot name a directory")
    if version and not _SAFE_VERSION.match(version):
        raise ValueError(f"{version!r} cannot name a directory")
    return Path(data_dir) / DIRNAME / (f"{slug}@{version}" if version else slug)


def ensure(data_dir: Path, slug: str, version: str = "") -> Path | None:
    """The directory, created if it was not there. None if it could not be.

    A hub whose volume is read-only should still serve every backend that does
    not need storage, so this reports failure rather than raising.
    """
    try:
        path = path_for(data_dir, slug, version)
    except ValueError:
        log.warning("no storage for backend %r: its name cannot be a directory", slug)
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create storage for backend %r at %s: %s", slug, path, exc)
        return None
    return path


def expand(value: str, storage: Path | str) -> str:
    """Replace `$MCPHUB_STORAGE` / `${MCPHUB_STORAGE}` in a configured value.

    A launched server is handed its environment directly, with no shell in
    between, so nothing else would ever expand it — and `DB_PATH=$MCPHUB_STORAGE/x.db`
    is how someone will write it whether or not it works.
    """
    return _REFERENCE.sub(str(storage).replace("\\", "\\\\"), value)


def rename(data_dir: Path, old: str, new: str) -> None:
    """Follow a backend that was given a new slug, so its files go with it."""
    if old == new:
        return
    try:
        source, target = path_for(data_dir, old), path_for(data_dir, new)
    except ValueError:
        return
    if not source.is_dir() or target.exists():
        return
    try:
        source.rename(target)
        log.info("moved storage for %r to %r", old, new)
    except OSError as exc:
        log.warning("could not move storage from %s to %s: %s", source, target, exc)
