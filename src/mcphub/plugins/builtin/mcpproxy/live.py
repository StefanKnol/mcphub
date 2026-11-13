"""Resources and prompts listed live, with the save-time catalogue as the fallback.

Tools are mirrored from the catalogue cached when the backend is saved, and
that is deliberate: a tool's schema is what an account's level and the tool
selection are decided against. Resources and prompts are not decided against
anything (see `roles.py`: levels apply to tools only, and the tool selection
does not filter them), and an upstream's list of them can be dynamic. A server
that publishes one document per story run gains a resource every run, and with
the catalogue alone none of them would reach a client until an administrator
saved the backend again.

So `resources/list` and `prompts/list` ask the upstream when a client asks,
once per request and within a short timeout, and offer what it lists now. If
the upstream cannot be asked (down, slow, or it does not offer the capability)
the answer is the cached catalogue, exactly as before, rather than an error.

Reading is through on demand either way. A resource or prompt in the catalogue
is served by its mirrored entry, unchanged, which is what keeps an MCP Apps
`ui://` resource carrying its cached mime type and `_meta.ui` policy. One the
catalogue does not hold is read through when the upstream lists it now.

The seam is a subclass overriding the server's public `list_resources`,
`read_resource`, `list_prompts` and `get_prompt`. The SDK's request handlers
call exactly those methods, so nothing private is reached into, and the
mirrored entries keep going through the SDK's own managers via `super()`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from .upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)

LIVE_LIST_TIMEOUT_SECONDS = 5.0
"""The most a client's list waits on the upstream before the cache answers.
Capped further by the backend's own timeout when that is shorter."""


class ProxiedServer(MCPServer):
    """The server a proxied backend mounts: the catalogue, plus a live list."""

    def __init__(self, *arguments: Any, upstream: Upstream,
                 live_list_timeout_seconds: float = LIVE_LIST_TIMEOUT_SECONDS,
                 **keyword_arguments: Any) -> None:
        super().__init__(*arguments, **keyword_arguments)
        self._upstream = upstream
        self._live_list_timeout_seconds = live_list_timeout_seconds
        self._resource_uris_listed_live: set[str] = set()
        self._prompt_names_listed_live: set[str] = set()

    async def _ask_upstream(self, what: str, ask: Any) -> list[Any] | None:
        """One upstream list call within the timeout, or None if it gave no answer."""
        try:
            return list(await asyncio.wait_for(ask(), timeout=self._live_list_timeout_seconds))
        except asyncio.TimeoutError:
            log.info("%s: the upstream did not list its %s in time; offering the cached ones",
                     self.name, what)
        except UpstreamError as exc:
            log.info("%s: could not list the upstream's %s (%s); offering the cached ones",
                     self.name, what, exc)
        except Exception:  # noqa: BLE001 - the cached list is always a better answer than an error
            log.exception("%s: listing the upstream's %s failed; offering the cached ones",
                          self.name, what)
        return None

    # ── resources ─────────────────────────────────────────────────────────

    async def _list_resources_live(self) -> list[Any] | None:
        listed = await self._ask_upstream("resources", self._upstream.list_resources_once)
        if listed is not None:
            self._resource_uris_listed_live = {str(resource.uri) for resource in listed}
        return listed

    async def list_resources(self) -> list[Any]:
        listed = await self._list_resources_live()
        if listed is None:
            return await super().list_resources()
        # The upstream's own records, copied: name, title, mime type, `_meta`
        # and the rest travel as the upstream wrote them.
        return [resource.model_copy(deep=True) for resource in listed]

    async def read_resource(self, uri: Any, context: Any = None) -> Any:
        uri_text = str(uri)
        cached = {str(resource.uri) for resource in await super().list_resources()}
        if uri_text in cached:
            return await super().read_resource(uri, context)
        if uri_text not in self._resource_uris_listed_live:
            # Asked for before anyone listed it here, or it is new since: the
            # upstream says whether it lists it now. A uri it does not list is
            # not read, as before this was live.
            await self._list_resources_live()
            if uri_text not in self._resource_uris_listed_live:
                raise ResourceNotFoundError(f"Unknown resource: {uri_text}")
        try:
            result = await self._upstream.read_resource(uri_text)
        except UpstreamError as exc:
            raise ResourceError(f"{uri_text}: {exc}") from exc
        return [_contents_of(content) for content in (getattr(result, "contents", None) or [])]

    # ── prompts ───────────────────────────────────────────────────────────

    async def _list_prompts_live(self) -> list[Any] | None:
        listed = await self._ask_upstream("prompts", self._upstream.list_prompts_once)
        if listed is not None:
            self._prompt_names_listed_live = {prompt.name for prompt in listed}
        return listed

    async def list_prompts(self) -> list[Any]:
        listed = await self._list_prompts_live()
        if listed is None:
            return await super().list_prompts()
        return [prompt.model_copy(deep=True) for prompt in listed]

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None,
                         context: Any = None) -> Any:
        cached = {prompt.name for prompt in await super().list_prompts()}
        if name in cached:
            return await super().get_prompt(name, arguments, context)
        if name not in self._prompt_names_listed_live:
            await self._list_prompts_live()
            if name not in self._prompt_names_listed_live:
                raise ValueError(f"Unknown prompt: {name}")
        supplied = {key: str(value) for key, value in (arguments or {}).items() if value is not None}
        try:
            return await self._upstream.get_prompt(name, supplied)
        except UpstreamError as exc:
            raise ValueError(f"{name}: {exc}") from exc


def _contents_of(content: Any) -> ReadResourceContents:
    """One upstream content block as the server hands it on.

    A blob is decoded to bytes, so the SDK encodes it as a blob again on the
    way out rather than passing the base64 on as if it were text.
    """
    meta = getattr(content, "meta", None)
    mime_type = getattr(content, "mime_type", None)
    text = getattr(content, "text", None)
    if text is not None:
        return ReadResourceContents(content=text, mime_type=mime_type, meta=meta)
    blob = getattr(content, "blob", None) or ""
    try:
        return ReadResourceContents(content=base64.b64decode(blob, validate=True),
                                    mime_type=mime_type, meta=meta)
    except (binascii.Error, ValueError):
        return ReadResourceContents(content=blob, mime_type=mime_type, meta=meta)
