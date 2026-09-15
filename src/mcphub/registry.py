"""Discovery through the official MCP registry.

The registry publishes a `server.json` per server describing how to run it and
what configuration it takes — which package on npm or PyPI, which runtime, and
every environment variable with its description, whether it is required and
whether it is secret.

That is considerably more than a list of tags would give: it is enough to
generate both the command line and a correctly typed settings form, so adding
a published server becomes searching for it rather than knowing its package
name and reading its README.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import anyio

from .verified import Verification, lookup as lookup_verification

log = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
MAX_LIMIT = 100
"""The registry rejects a larger `limit` with 422 rather than clamping it."""
OFFICIAL_META = "io.modelcontextprotocol.registry/official"

# What each registry type is launched with when the entry does not say.
RUNTIME_HINTS = {"npm": "npx", "pypi": "uvx"}


class RegistryError(RuntimeError):
    """The registry could not be reached or understood."""


@dataclass(frozen=True)
class EnvVar:
    name: str
    description: str = ""
    required: bool = False
    secret: bool = False


@dataclass(frozen=True)
class PackageRef:
    """Enough to rebuild the launch command at a chosen version."""

    registry_type: str
    identifier: str
    runtime: str
    args: tuple[str, ...] = ()

    def command(self, version: str = "") -> str:
        """The command line, optionally pinned.

        The two registries spell a pin differently, and getting it wrong is
        silent: `uvx pkg@1.2.3` is not an error, it is a request for a package
        whose name happens to contain an at-sign.
        """
        spec = self.identifier
        if version:
            if self.registry_type == "npm":
                spec = f"{self.identifier}@{version}"
            elif self.registry_type == "pypi":
                spec = f"{self.identifier}=={version}"
            else:
                spec = f"{self.identifier}:{version}"
        return " ".join([self.runtime, *self.args, spec]).strip()


@dataclass(frozen=True)
class ServerVersion:
    version: str
    is_latest: bool
    package: PackageRef | None


@dataclass(frozen=True)
class RegistryServer:
    name: str
    title: str
    description: str
    version: str
    repository: str = ""
    command: str = ""
    """Ready-to-run command line, empty for a remote-only server."""
    remote_url: str = ""
    """Set instead of `command` when the server is hosted rather than launched."""
    env: tuple[EnvVar, ...] = field(default_factory=tuple)
    icons: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    package: PackageRef | None = None

    @property
    def verification(self) -> Verification | None:
        """What was observed when this server was last launched here, if ever.

        Deliberately not read from the registry entry: a compatibility flag set
        by the server's own author would be a claim, and the point of this is
        that something measured it.
        """
        return lookup_verification(self.name)

    @property
    def slug_hint(self) -> str:
        """A sensible default URL name, derived from the last path segment."""
        tail = self.name.rsplit("/", 1)[-1]
        cleaned = "".join(c if c.isalnum() else "-" for c in tail.lower()).strip("-")
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned[:40] or "server"

    @property
    def runnable(self) -> bool:
        return bool(self.command or self.remote_url)


def _package_ref(package: dict[str, Any]) -> PackageRef | None:
    """The launchable package from a registry entry, if it has one."""
    registry_type = package.get("registryType", "")
    # `runtimeHint` is frequently absent, so the registry type decides.
    runtime = package.get("runtimeHint") or RUNTIME_HINTS.get(registry_type, "")
    identifier = package.get("identifier", "")
    if not runtime or not identifier:
        return None
    args = tuple(
        str(a.get("value"))
        for a in package.get("runtimeArguments") or []
        if a.get("value") is not None
    )
    return PackageRef(registry_type=registry_type, identifier=identifier, runtime=runtime, args=args)


def _build_command(package: dict[str, Any]) -> str:
    """Compose the unpinned command line the registry entry describes."""
    ref = _package_ref(package)
    return ref.command() if ref else ""


def _parse(entry: dict[str, Any]) -> RegistryServer | None:
    server = entry.get("server") or {}
    name = server.get("name")
    if not name:
        return None

    command, env, package_ref = "", (), None
    # Prefer a package we can actually launch over one we cannot.
    for package in server.get("packages") or []:
        ref = _package_ref(package)
        if ref is None:
            continue
        command, package_ref = ref.command(), ref
        env = tuple(
            EnvVar(
                name=v["name"],
                description=v.get("description", ""),
                required=bool(v.get("isRequired")),
                secret=bool(v.get("isSecret")),
            )
            for v in package.get("environmentVariables") or []
            if v.get("name")
        )
        break

    remote_url = ""
    if not command:
        for remote in server.get("remotes") or []:
            if remote.get("type") in ("streamable-http", "http") and remote.get("url"):
                remote_url = remote["url"]
                break

    icons = tuple(i for i in (server.get("icons") or []) if isinstance(i, dict) and i.get("src"))

    return RegistryServer(
        name=name,
        title=server.get("title") or name.rsplit("/", 1)[-1],
        description=server.get("description", ""),
        version=server.get("version", ""),
        repository=(server.get("repository") or {}).get("url", ""),
        command=command,
        remote_url=remote_url,
        env=env,
        icons=icons,
        package=package_ref,
    )


def _latest_only(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the one-row-per-version listing down to one row per server.

    A search for a popular term returns every published version — 36 rows for
    two servers, in one measurement — so without this the results are mostly
    duplicates.
    """
    best: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = (entry.get("server") or {}).get("name")
        if not name:
            continue
        meta = (entry.get("_meta") or {}).get(OFFICIAL_META) or {}
        if meta.get("isLatest") or name not in best:
            if name in best:
                prior = (best[name].get("_meta") or {}).get(OFFICIAL_META) or {}
                if prior.get("isLatest") and not meta.get("isLatest"):
                    continue
            best[name] = entry
    return list(best.values())


def _fetch(params: dict[str, str]) -> dict[str, Any]:
    url = f"{REGISTRY_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RegistryError(f"The registry returned {exc.code} for that search.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RegistryError(f"Could not reach the MCP registry: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError("The registry returned something that was not JSON.") from exc


async def search(query: str, limit: int = 30) -> list[RegistryServer]:
    """Search the registry, newest version of each server only.

    Note the parameter is `search`. Passing `q` is accepted with a 200 and the
    *unfiltered* list, which looks like a search that matches everything.
    """
    params = {"limit": str(max(1, min(limit * 4, MAX_LIMIT)))}
    if query.strip():
        params["search"] = query.strip()

    payload = await anyio.to_thread.run_sync(lambda: _fetch(params))
    entries = _latest_only(payload.get("servers") or [])

    servers = [s for s in (_parse(e) for e in entries) if s is not None]
    # A server we cannot launch or connect to is noise in a list whose only
    # purpose is adding one.
    servers = [s for s in servers if s.runnable]
    servers.sort(key=lambda s: s.name)
    return servers[:limit]


async def get(name: str) -> RegistryServer | None:
    for server in await search(name, limit=60):
        if server.name == name:
            return server
    return None


async def versions(name: str, limit: int = MAX_LIMIT) -> list[ServerVersion]:
    """Every published version of one server, newest first.

    Taken from the registry rather than from PyPI or npm: the registry lists
    one row per published version, and those are the versions actually released
    *as an MCP server*, which is not always every release of the package.
    """
    payload = await anyio.to_thread.run_sync(
        lambda: _fetch({"search": name, "limit": str(min(limit, MAX_LIMIT))})
    )
    found: dict[str, ServerVersion] = {}
    for entry in payload.get("servers") or []:
        server = entry.get("server") or {}
        if server.get("name") != name:
            continue
        version = str(server.get("version") or "")
        if not version or version in found:
            continue
        meta = (entry.get("_meta") or {}).get(OFFICIAL_META) or {}
        package = next(
            (ref for ref in (_package_ref(p) for p in server.get("packages") or []) if ref),
            None,
        )
        found[version] = ServerVersion(version, bool(meta.get("isLatest")), package)

    return sorted(found.values(), key=lambda v: _sortable(v.version), reverse=True)


def _sortable(version: str) -> tuple[Any, ...]:
    """Order versions numerically where possible, textually where not.

    Published versions are not reliably semver — `0.15.0.0` and `1.0.0-rc1`
    both occur — so each dot-separated part sorts as a number when it is one.
    """
    parts: list[Any] = []
    for chunk in re.split(r"[.\-+]", version):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)
