"""What an account may do with a backend, beyond whether it may reach it.

A grant answers "may this account reach this backend at all". A level answers
"how far", and there are three presets:

    viewer   only tools the server marks as making no changes
    user     everything except tools the server marks as destructive
    admin    everything

For an MCP backend the hub enforces this itself, from the annotations servers
already publish, rather than asking the server to. That works on a server that
has never heard of this hub — and enforcement that depends on the other side
cooperating is not enforcement. It is applied to tools only: resources and
prompts are read-shaped by nature, and nothing in the protocol marks one of
them dangerous.

A tool carrying no annotations is the interesting case. It is withheld from a
viewer (nothing says it only reads) and allowed for a user (nothing says it
destroys). Erring the other way would either make `viewer` useless on the many
servers that annotate nothing, or make `user` a promise the hub cannot keep —
and of the two mistakes, a level that is quietly too permissive is the worse
one to ship.

The level is also handed to a trusted app as `X-Mcphub-Role`, for the things
the hub has no way to judge.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import replace
from typing import Any

from mcp_types import INVALID_PARAMS
from mcp.shared.exceptions import MCPError

log = logging.getLogger(__name__)

VIEWER, USER, ADMIN = "viewer", "user", "admin"
LEVELS = (VIEWER, USER, ADMIN)
DEFAULT = USER

DESCRIPTIONS = {
    VIEWER: "Read-only. Only tools the backend marks as making no changes.",
    USER: "Everything except tools the backend marks as destructive.",
    ADMIN: "Everything the backend offers.",
}

#: Set on the ASGI scope by the hub's per-request authorization check, and read
#: back inside MCP request handling. The scope is the carrier that matters; the
#: contextvar is a fallback for the day the SDK stops handing the transport's
#: request down to middleware (it is flagged internal there).
SCOPE_KEY = "mcphub.role"
current_role: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcphub_role", default=None
)


def normalise(level: str | None) -> str:
    return level if level in LEVELS else DEFAULT


#: The same hints under the spelling a serialised result carries them in.
_WIRE = {"read_only_hint": "readOnlyHint", "destructive_hint": "destructiveHint"}


def _hint(tool: Any, name: str) -> bool | None:
    """One annotation off a tool, whether it is still a model or already JSON.

    Both shapes reach here. `tools/list` is answered as plain dicts by the time
    a middleware sees it, while a lookup straight off the server hands back
    models — so reading only attributes silently found no annotations at all,
    and a viewer was offered nothing anywhere.
    """
    if isinstance(tool, dict):
        annotations = tool.get("annotations")
    else:
        annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        value = annotations.get(_WIRE[name], annotations.get(name))
    else:
        value = getattr(annotations, name, None)
    return value if isinstance(value, bool) else None


def allows(level: str, tool: Any) -> bool:
    """Whether `level` may use this tool, judged from what the backend declared."""
    level = normalise(level)
    if level == ADMIN:
        return True
    if level == USER:
        # Only a tool that says outright that it is destructive is withheld.
        return _hint(tool, "destructive_hint") is not True
    # viewer: a tool has to say that it only reads.
    return _hint(tool, "read_only_hint") is True


def permitted(level: str, tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if allows(level, tool)]


def refusal(level: str, tool_name: str) -> str:
    level = normalise(level)
    return (
        f"{tool_name!r} is not available at this account's level for this backend "
        f"({level}). {DESCRIPTIONS[level]}"
    )


class RoleGuard:
    """Applies the calling account's level to one running backend.

    Registered as `ServerMiddleware` on every variant, so one process serves
    every level rather than one process per (backend, version, level) — the
    difference between a handful of subprocesses and dozens.

    Both halves matter. Filtering `tools/list` is what keeps a tool out of the
    model's context, which is the useful part; refusing `tools/call` is what
    makes it a rule rather than a suggestion, since a client can call a tool it
    was never offered.
    """

    def __init__(self, server: Any, slug: str) -> None:
        self._server = server
        self._slug = slug

    def level_for(self, ctx: Any) -> str:
        request = getattr(ctx, "request", None)
        scope = getattr(request, "scope", None)
        if isinstance(scope, dict) and scope.get(SCOPE_KEY):
            return normalise(str(scope[SCOPE_KEY]))
        stashed = current_role.get()
        if stashed:
            return normalise(stashed)
        # Nothing identified the caller. Every route into a mounted backend
        # passes the hub's authorization check, which sets both carriers, so
        # this means the plumbing changed underneath us — answer with the
        # narrowest level rather than the widest.
        log.warning("no level on a request to backend %s; treating it as %s",
                    self._slug, VIEWER)
        return VIEWER

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/list":
            return self._filter(await call_next(ctx), self.level_for(ctx))
        if ctx.method == "tools/call":
            await self._check(ctx)
        return await call_next(ctx)

    async def _check(self, ctx: Any) -> None:
        level = self.level_for(ctx)
        if level == ADMIN:
            return
        name = (ctx.params or {}).get("name")
        if not isinstance(name, str):
            return  # Malformed; let params validation say so in its own words.
        tool = next((t for t in await self._tools() if t.name == name), None)
        if tool is None:
            return  # Unknown tool: the handler's own error is the better one.
        if not allows(level, tool):
            log.info("backend %s refused %r at level %s", self._slug, name, level)
            raise MCPError(INVALID_PARAMS, refusal(level, name))

    async def _tools(self) -> list[Any]:
        try:
            return list(await self._server.list_tools())
        except Exception:  # noqa: BLE001 - a lookup failure must not open the gate
            log.exception("backend %s could not list its tools while checking a level",
                          self._slug)
            raise MCPError(INVALID_PARAMS,
                           "This backend could not be asked what this tool does, "
                           "so it was not run.") from None

    def _filter(self, result: Any, level: str) -> Any:
        """Keep only the tools this level may use, and stop the answer being shared.

        The cache scope is narrowed whatever the level, including admin. It is
        the backend's own declaration, made without knowing the hub would hand
        different answers to different accounts — leaving a `public` scope on
        an administrator's full list is how a viewer ends up being served it
        out of something in between.
        """
        tools = _tools_of(result)
        if tools is None:
            return result
        kept = tools if level == ADMIN else permitted(level, tools)
        return _rebuild(result, kept)


def _tools_of(result: Any) -> list[Any] | None:
    if isinstance(result, dict):
        found = result.get("tools")
        return found if isinstance(found, list) else None
    found = getattr(result, "tools", None)
    return found if isinstance(found, list) else None


def _rebuild(result: Any, tools: list[Any]) -> Any:
    """A copy of a `tools/list` result with these tools and a private cache scope."""
    if isinstance(result, dict):
        return {**result, "tools": tools, "cacheScope": "private"}
    copy = getattr(result, "model_copy", None)
    if copy is not None:
        return copy(update={"tools": tools, "cache_scope": "private"})
    return replace(result, tools=tools, cache_scope="private")
