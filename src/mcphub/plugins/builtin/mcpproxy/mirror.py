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
        # The upstream already decided its output shape; re-deriving one from
        # our synthesised `-> str` would advertise a schema that is not true.
        structured_output=False,
    )
    return True
