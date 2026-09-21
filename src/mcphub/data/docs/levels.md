# Levels

A grant says whether an account may reach a backend, and its level says how far.

| Level | |
| --- | --- |
| `viewer` | Only tools the backend marks as making no changes. |
| `user` | Everything except tools the backend marks as destructive. |
| `admin` | Everything the backend offers. |

An administrator of the hub holds every backend at `admin` without a grant row.

## Over MCP: enforced

The hub filters `tools/list` to what the level allows and refuses a
`tools/call` for anything above it, reading `readOnlyHint` and
`destructiveHint` from your own tool declarations. See `annotations` — that
page is the one that decides whether levels do anything useful for your server.

Things worth knowing if you are building against this:

- **One process serves every level.** The filtering is per request, not per
  server, so levels cost no extra subprocesses and nothing about the level
  reaches your code.
- **The level is read on every request.** Changing it lands on the next call of
  an already-open connector, not whenever the client reconnects.
- **Tools only.** Resources and prompts are read-shaped by nature, and nothing
  in the protocol marks one of them dangerous, so they are not filtered.
- **A refused call is an error, not an empty result.** The message names the
  tool and the level, so a person reading their client's error can see why.

## Over HTTP: reported

A trusted web interface is sent `X-Mcphub-Role`. The hub does not enforce it,
because over HTTP it sees a method and a path and cannot tell an edit from a
search.

If your app has actions worth separating, read the header and act on it:

```python
ROLE_ORDER = {"viewer": 0, "user": 1, "admin": 2}

def at_least(request, level):
    have = ROLE_ORDER.get(request.headers.get("X-Mcphub-Role", "viewer"), 0)
    return have >= ROLE_ORDER[level]
```

Treat a missing header as `viewer`. It is missing when the app is sandboxed,
and a sandboxed app is one nobody vouched for.

## Choosing what your levels mean

The three names are fixed, and the mapping from annotations is fixed, so the
design work is in your annotations rather than in configuration. If you find
yourself wanting a fourth level, the question to ask first is whether the tool
in question is really destructive, or really read-only, and has been marked
wrongly.
