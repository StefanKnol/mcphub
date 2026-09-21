# Annotate your tools

This is the one thing worth doing before anything else on this list.

mcphub gives each grant a level — `viewer`, `user` or `admin` — and enforces it
by reading the annotations your server already publishes. It does not ask your
server to check anything, because enforcement that depends on the other side
cooperating is not enforcement. It reads what you declared.

## The two hints that matter

| Hint | Say `true` when |
| --- | --- |
| `readOnlyHint` | The tool changes nothing. Reading, listing, searching. |
| `destructiveHint` | The tool can destroy or overwrite something a person would mind losing. |

The hub maps them like this:

| Level | Gets |
| --- | --- |
| `viewer` | Only tools with `readOnlyHint: true` |
| `user` | Everything except tools with `destructiveHint: true` |
| `admin` | Everything |

A tool above the level is left out of `tools/list` **and** refused if called
anyway — a client can call a tool it was never offered.

## What happens if you annotate nothing

A tool with no annotations is withheld from a viewer and allowed for a user.
Nothing says it only reads, and nothing says it destroys.

So a server that annotates nothing at all offers a viewer nothing at all. That
is not a bug to work around; it is the honest answer to "which of these are safe
for someone who may only read?" when the server has not said.

## In the Python SDK

```python
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("dictionary")

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup_word(word: str) -> str:
    """Definition of one word."""

@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def propose_word(word: str, meaning: str) -> str:
    """Add a candidate. Reviewed before it becomes canon."""

@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def discard_candidate(word: str) -> str:
    """Remove a candidate outright."""
```

Be accurate rather than generous. `destructiveHint: true` on something merely
additive costs your users the `user` level for no reason; leaving it off
something that deletes silently hands deletion to everyone above `viewer`.

## Adding to something, not deleting from it

`destructiveHint` is about *irreversible loss*, not about writing. Adding a
firewall rule is a write and not destructive. Removing one is destructive.
Reordering them is destructive if the order cannot be recovered.

Setting `idempotentHint` and `openWorldHint` is good practice but the hub does
not read them.
