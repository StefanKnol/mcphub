"""Re-expose an upstream server's tools as tools of our own.

The SDK builds a tool's input schema by inspecting a Python function's
signature — there is no way to hand it a JSON Schema directly. So rather than
fight that, this synthesises a function whose signature *produces* the upstream
schema when inspected, and the SDK's normal path does the rest.

That round trip is lossy for deeply nested schemas. It is exact for the flat
object schemas that tools almost always use, which is every one of the 126
tools on the Unraid agent.
"""

from __future__ import annotations

import inspect
import json
import keyword
import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts import Prompt
from mcp.server.mcpserver.prompts.base import PromptArgument
from mcp.server.mcpserver.resources import FunctionResource
from mcp.types import Prompt as UpstreamPrompt
from mcp.types import Resource as UpstreamResource
from mcp.types import Tool as UpstreamTool
from mcp.types import ToolAnnotations
from pydantic import Field

from .upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)

_SCALARS: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _annotation(schema: dict[str, Any]) -> Any:
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        try:
            return Literal[tuple(schema["enum"])]  # type: ignore[misc]
        except TypeError:
            return str
    declared = schema.get("type")
    if isinstance(declared, list):  # e.g. ["string", "null"]
        real = [t for t in declared if t != "null"]
        base = _SCALARS.get(real[0], Any) if real else Any
        return base | None if "null" in declared else base
    return _SCALARS.get(declared, Any)


def _usable_name(name: str) -> bool:
    """The parameter has to become a real Python parameter to be inspectable."""
    return name.isidentifier() and not keyword.iskeyword(name) and name != "ctx"


def _build_signature(schema: dict[str, Any]) -> tuple[inspect.Signature, dict[str, Any], list[str]]:
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or ())

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    skipped: list[str] = []

    # Required first: Python forbids a parameter without a default after one
    # with a default, even keyword-only ones read better this way.
    ordered = sorted(properties.items(), key=lambda kv: kv[0] not in required)

    for name, prop in ordered:
        if not isinstance(prop, dict) or not _usable_name(name):
            skipped.append(name)
            continue
        base = _annotation(prop)
        description = prop.get("description") or ""
        if name in required:
            annotation = Annotated[base, Field(description=description)]
            default = inspect.Parameter.empty
        else:
            annotation = Annotated[base | None, Field(default=None, description=description)]
            default = None
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)
        )
        annotations[name] = annotation

    annotations["return"] = str
    return inspect.Signature(params, return_annotation=str), annotations, skipped


def _flatten(result: Any) -> str:
    """Render an upstream CallToolResult as text.

    Structured content is preferred when there is no text to show; anything
    else (an image, an embedded resource) is described rather than dropped
    silently, so a caller is never left thinking a tool returned nothing.
    """
    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
        else:
            chunks.append(f"[{getattr(block, 'type', 'content')} omitted by the proxy]")
    if chunks:
        return "\n".join(chunks)

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, indent=2, default=str)
    return ""


def _annotations_for(tool: UpstreamTool) -> ToolAnnotations | None:
    """Carry the upstream's own risk hints through.

    These drive the client's permission prompts, so losing them would quietly
    turn a destructive upstream tool into one that looks harmless.
    """
    source = tool.annotations
    if source is None:
        return None
    return ToolAnnotations(
        title=source.title or tool.name,
        read_only_hint=source.read_only_hint,
        destructive_hint=source.destructive_hint,
        idempotent_hint=source.idempotent_hint,
        open_world_hint=source.open_world_hint,
    )


def mirror_tool(mcp: MCPServer, upstream: Upstream, tool: UpstreamTool) -> bool:
    """Register one upstream tool on our server. Returns False if it was skipped."""
    schema = tool.input_schema or {}
    try:
        signature, annotations, skipped = _build_signature(schema)
    except (TypeError, ValueError):
        log.exception("could not mirror tool %s", tool.name)
        return False

    if skipped:
        log.warning("tool %s: parameters not representable, dropped: %s", tool.name, ", ".join(skipped))

    name = tool.name

    async def forward(**kwargs: Any) -> str:
        # Arguments the caller did not supply are dropped rather than sent as
        # nulls, which some servers treat as "clear this field".
        arguments = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await upstream.call_tool(name, arguments)
        except UpstreamError as exc:
            raise ToolError(str(exc)) from exc
        if getattr(result, "is_error", False):
            raise ToolError(_flatten(result) or f"{name} failed upstream.")
        return _flatten(result)

    forward.__name__ = name
    forward.__doc__ = tool.description or f"Proxied tool {name}."
    forward.__signature__ = signature  # type: ignore[attr-defined]
    forward.__annotations__ = annotations

    description = tool.description or ""
    if skipped:
        description += (
            f"\n\nNote: this proxy could not represent these parameters, so they "
            f"cannot be set here: {', '.join(skipped)}."
        )

    mcp.add_tool(
        forward,
        name=name,
        title=tool.title,
        description=description or None,
        annotations=_annotations_for(tool),
        # Carries the MCP Apps binding (`_meta.ui.resourceUri`) among anything
        # else the upstream attached. Dropping it silently turns a tool with a
        # user interface into a plain one, with nothing to indicate why.
        meta=tool.meta,
        # The upstream already decided its output shape; re-deriving one from
        # our synthesised `-> str` would advertise a schema that is not true.
        structured_output=False,
    )
    return True


def _resource_text(result: Any) -> str:
    """Flatten a ReadResourceResult into what FunctionResource returns."""
    for content in getattr(result, "contents", None) or []:
        text = getattr(content, "text", None)
        if text is not None:
            return text
        blob = getattr(content, "blob", None)
        if blob is not None:
            return blob
    return ""


def mirror_resource(mcp: MCPServer, upstream: Upstream, resource: UpstreamResource) -> bool:
    """Re-expose one upstream resource, fetched on read rather than cached.

    MCP Apps serves a tool's interface as a `ui://` resource, so a proxy that
    forwards tools but not resources hands the client a tool pointing at an
    interface it cannot fetch. Contents are read through on demand: a UI
    resource can change with the upstream without this hub being resaved.
    """
    uri = str(resource.uri)

    async def read() -> str:
        try:
            return _resource_text(await upstream.read_resource(uri))
        except UpstreamError as exc:
            raise ValueError(f"{uri}: {exc}") from exc

    try:
        mcp.add_resource(FunctionResource(
            uri=uri,
            name=resource.name or uri,
            title=resource.title,
            description=resource.description,
            mime_type=resource.mime_type,
            # The `text/html;profile=mcp-app` type and the `_meta.ui` block
            # (csp, permissions) are what let a host render this safely, so
            # they travel with it.
            meta=resource.meta,
            fn=read,
        ))
    except Exception:  # noqa: BLE001 - one bad resource must not stop the rest
        log.exception("could not mirror resource %s", uri)
        return False
    return True


def mirror_prompt(mcp: MCPServer, upstream: Upstream, prompt: UpstreamPrompt) -> bool:
    """Re-expose one upstream prompt, rendered on demand.

    Arguments are declared from the upstream's own list rather than derived
    from a Python signature, so a prompt keeps the names, descriptions and
    required flags it was published with.
    """
    name = prompt.name

    async def render(**kwargs: Any) -> Any:
        supplied = {k: str(v) for k, v in kwargs.items() if v is not None}
        try:
            result = await upstream.get_prompt(name, supplied)
        except UpstreamError as exc:
            raise ValueError(f"{name}: {exc}") from exc
        # Dumped to dicts on purpose. The renderer keeps its own `Message`
        # type or a dict, and anything else falls through to a branch that
        # JSON-encodes the whole thing into one message body — so returning
        # the upstream's `PromptMessage` objects directly produces a prompt
        # whose content is a printed dump of itself.
        return [
            m.model_dump(by_alias=True, exclude_none=True)
            for m in (getattr(result, "messages", None) or [])
        ]

    try:
        mcp.add_prompt(Prompt(
            name=name,
            title=prompt.title,
            description=prompt.description,
            arguments=[
                PromptArgument(
                    name=a.name,
                    description=a.description,
                    required=bool(a.required),
                )
                for a in (prompt.arguments or [])
            ],
            fn=render,
        ))
    except Exception:  # noqa: BLE001 - one bad prompt must not stop the rest
        log.exception("could not mirror prompt %s", name)
        return False
    return True
