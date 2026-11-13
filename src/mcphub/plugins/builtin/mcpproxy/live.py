"""Resources and prompts listed live, with the save-time catalogue as the fallback.

Tools are mirrored from the catalogue cached when the backend is saved, and
that is deliberate: a tool's schema is what an account's level and the tool
selection are decided against. Resources and prompts are not decided against
anything (see `roles.py`: levels apply to tools only, and the tool selection
does not filter them), and an upstream's list of them can be dynamic. A server
that publishes one document per story run gains a resource every run, and with
the catalogue alone none of them would reach a client until an administrator
saved the backend again.

So `resources/list`, `resources/templates/list` and `prompts/list` ask the
upstream when a client asks, once per request and within a short timeout, and
offer what it lists now. If the upstream cannot be asked (down, slow, or it
does not offer the capability) the answer is the cached catalogue, exactly as
before, rather than an error. After a list the upstream gave no answer to, the
cache answers every list for a short while without asking: a stdio server that
is slow to start would otherwise hold each list at connect for the whole
timeout, and one that crashes on start would be launched again for every list.

A resource in the catalogue is listed and read as its mirrored entry, so the
two agree, and an MCP Apps `ui://` resource keeps the mime type and `_meta.ui`
policy it was saved with whatever the upstream says now. Any other uri is read
through to the upstream, which is the one that knows whether it can read it:
a uri it stopped listing, or one a template of its own describes, reads as it
would straight from the upstream, and one it cannot read comes back with the
upstream's own error. A prompt is always rendered by the upstream, so its
answer (the description included) is the upstream's own.

`completion/complete` is passed through to the upstream too, so a client's
completion of a prompt's or a template's argument keeps working behind the hub.

The seam is a subclass overriding the server's public `list_resources`,
`read_resource`, `list_resource_templates`, `list_prompts` and `get_prompt`.
The SDK's request handlers call exactly those methods, so nothing private is
reached into, and the mirrored entries keep going through the SDK's own
managers via `super()`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import Callable, Iterable
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR

from .upstream import Upstream, UpstreamError, UpstreamRefusal

log = logging.getLogger(__name__)

LIVE_LIST_TIMEOUT_SECONDS = 5.0
"""The most a client's list waits on the upstream before the cache answers.
Capped further by the backend's own timeout when that is shorter."""

LIVE_LIST_PAUSE_SECONDS = 30.0
"""How long the cache answers every list without asking, after a list the
upstream gave no answer to."""


def _first_of_each(records: Iterable[Any], key_of: Callable[[Any], str]) -> list[Any]:
    """The records with a repeated key dropped, the first one kept.

    The catalogue does the same (the SDK's managers keep the first entry of a
    uri or a name), so a list read live says what a list read from it would.
    """
    seen: set[str] = set()
    kept: list[Any] = []
    for record in records:
        key = key_of(record)
        if key in seen:
            continue
        seen.add(key)
        kept.append(record)
    return kept


class ProxiedServer(MCPServer):
    """The server a proxied backend mounts: the catalogue, plus a live list."""

    def __init__(self, *arguments: Any, upstream: Upstream,
                 live_list_timeout_seconds: float = LIVE_LIST_TIMEOUT_SECONDS,
                 live_list_pause_seconds: float = LIVE_LIST_PAUSE_SECONDS,
                 **keyword_arguments: Any) -> None:
        super().__init__(*arguments, **keyword_arguments)
        self._upstream = upstream
        self._live_list_timeout_seconds = live_list_timeout_seconds
        self._live_list_pause_seconds = live_list_pause_seconds
        self._not_asking_until = 0.0
        self.completion()(self._complete)

    async def _ask_upstream(self, what: str, ask: Callable[[], Any]) -> Any | None:
        """One upstream question within the timeout, or None if it gave no answer.

        A refusal is an answer (the upstream is there, and said no), so only a
        timeout or a failure to reach it starts the pause.
        """
        now = asyncio.get_running_loop().time()
        if now < self._not_asking_until:
            log.debug("%s: not asking the upstream for its %s yet", self.name, what)
            return None
        try:
            return await asyncio.wait_for(ask(), timeout=self._live_list_timeout_seconds)
        except UpstreamRefusal as exc:
            log.info("%s: the upstream would not give its %s (%s); offering the cached ones",
                     self.name, what, exc)
            return None
        except asyncio.TimeoutError:
            log.info("%s: the upstream did not give its %s in time; offering the cached ones",
                     self.name, what)
        except UpstreamError as exc:
            log.info("%s: could not ask the upstream for its %s (%s); offering the cached ones",
                     self.name, what, exc)
        except Exception:  # noqa: BLE001 - the cached list is always a better answer than an error
            log.exception("%s: asking the upstream for its %s failed; offering the cached ones",
                          self.name, what)
        self._not_asking_until = asyncio.get_running_loop().time() + self._live_list_pause_seconds
        return None

    # ── resources ─────────────────────────────────────────────────────────

    async def list_resources(self) -> list[Any]:
        listed = await self._ask_upstream("resources", self._upstream.list_resources_once)
        cached = {str(resource.uri): resource for resource in await super().list_resources()}
        if listed is None:
            return list(cached.values())
        # A uri the catalogue holds is offered as its mirrored entry, which is
        # what a read of it serves; any other as the upstream wrote it, copied.
        offered = [cached.get(str(resource.uri)) or resource.model_copy(deep=True)
                   for resource in listed]
        return _first_of_each(offered, lambda resource: str(resource.uri))

    async def list_resource_templates(self) -> list[Any]:
        listed = await self._ask_upstream("resource templates",
                                          self._upstream.list_resource_templates_once)
        if listed is None:
            return await super().list_resource_templates()
        return _first_of_each([template.model_copy(deep=True) for template in listed],
                              lambda template: template.uri_template)

    async def read_resource(self, uri: Any, context: Any = None) -> Any:
        uri_text = str(uri)
        cached = {str(resource.uri) for resource in await super().list_resources()}
        if uri_text in cached:
            return await super().read_resource(uri, context)
        try:
            result = await self._upstream.read_resource(uri_text)
        except UpstreamRefusal as exc:
            # The upstream's own answer, with its own code: "no such resource"
            # stays that, and is never said by the hub for an upstream it
            # could not reach.
            raise MCPError(exc.code, exc.message, {"uri": uri_text}) from exc
        except UpstreamError as exc:
            raise ResourceError(f"{uri_text}: {exc}") from exc
        return [_contents_of(content) for content in (getattr(result, "contents", None) or [])]

    # ── prompts ───────────────────────────────────────────────────────────

    async def list_prompts(self) -> list[Any]:
        listed = await self._ask_upstream("prompts", self._upstream.list_prompts_once)
        if listed is None:
            return await super().list_prompts()
        return _first_of_each([prompt.model_copy(deep=True) for prompt in listed],
                              lambda prompt: prompt.name)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None,
                         context: Any = None) -> Any:
        supplied = {key: str(value) for key, value in (arguments or {}).items() if value is not None}
        try:
            return await self._upstream.get_prompt(name, supplied)
        except UpstreamRefusal as exc:
            raise MCPError(exc.code, exc.message) from exc
        except UpstreamError as exc:
            raise MCPError(INTERNAL_ERROR, f"{name}: {exc}") from exc

    # ── completion ────────────────────────────────────────────────────────

    async def _complete(self, reference: Any, argument: Any, context: Any) -> Any:
        """The upstream's completion of an argument, or none if it gives none."""
        context_arguments = getattr(context, "arguments", None) if context is not None else None
        return await self._ask_upstream("completions", lambda: self._upstream.complete_once(
            reference, {"name": argument.name, "value": argument.value}, context_arguments))


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
