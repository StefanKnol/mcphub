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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import anyio

from .verified import Verification, lookup as lookup_verification

log = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
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


def _build_command(package: dict[str, Any]) -> str:
    """Compose the command line the registry entry describes."""
    runtime = package.get("runtimeHint") or RUNTIME_HINTS.get(package.get("registryType", ""), "")
    identifier = package.get("identifier", "")
    if not runtime or not identifier:
        return ""
    args = [
        str(a.get("value"))
        for a in package.get("runtimeArguments") or []
        if a.get("value") is not None
    ]
    return " ".join([runtime, *args, identifier])


def _parse(entry: dict[str, Any]) -> RegistryServer | None:
    server = entry.get("server") or {}
    name = server.get("name")
    if not name:
        return None

    command, env = "", ()
    # Prefer a package we can actually launch over one we cannot.
    for package in server.get("packages") or []:
        built = _build_command(package)
        if not built:
            continue
        command = built
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
    params = {"limit": str(max(1, min(limit * 4, 100)))}
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
