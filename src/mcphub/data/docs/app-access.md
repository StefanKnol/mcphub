# Letting your app use other backends

An app often needs another backend on the same hub. A dictionary that files an
issue, a dashboard that reads the router, an agent that calls three things at
once. Without help there are two bad ways to do that: be handed a credential by
hand, which nobody can then see or revoke in one place, or run as whoever is
using the app, which makes the level on the app's own connector meaningless.

So the hub gives your app an identity of its own, and an administrator grants
it backends at levels — the same picker as for a person, on your backend's
settings page.

## What you are handed

Credentials arrive on whichever channel the hub already has to you.

**A server the hub launches** finds them in its environment:

```
MCPHUB_URL=https://mcp.example.com
MCPHUB_BACKENDS={"router":{"url":"https://mcp.example.com/mcp/router","token":"...","level":"viewer"}}
```

**A server the hub connects to**, or a trusted web interface it serves, is sent
the same JSON in a header:

```
X-Mcphub-Backends: {"router":{"url":"...","token":"...","level":"viewer"}}
```

One token per backend, because a token here is bound to one endpoint. The rule
that stops a token for the router being replayed against the Unraid one applies
to your app's tokens too.

## Using one

An ordinary bearer token against an ordinary MCP endpoint:

```python
import json, os
import httpx

BACKENDS = json.loads(os.environ.get("MCPHUB_BACKENDS", "{}"))

async def call(backend: str, method: str, params: dict):
    entry = BACKENDS[backend]
    async with httpx.AsyncClient() as client:
        response = await client.post(
            entry["url"],
            headers={"Authorization": f"Bearer {entry['token']}",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        return response.json()
```

`level` tells you what that token will actually get. At `viewer` the target's
`tools/list` comes back filtered and a call to anything else is refused, so
there is no point building a feature on a tool the grant does not cover — check
`level`, or list the tools and see.

## Two things that will bite you otherwise

**The access is the app's, not the user's.** A `viewer` using an app that holds
`admin` on the router reaches the router as the app. That is the point — the app
is a service with its own authority — but it means your app is the only thing
that can apply the person's own level. It is told that level in
`X-Mcphub-Role`; see `levels`. The hub cannot apply it for you, because over
your API it sees a method and a path and cannot tell an edit from a search.

**Tokens are not durable.** They are minted when your app starts and the hub
keeps only hashes, so a restart of the hub rotates them, and changing a grant
revokes them. Read them at start-up, and if one starts failing with a 401, read
your environment again rather than treating it as fatal.

## Asking for access

You cannot grant yourself anything; an administrator does it on your backend's
settings page. What you can do is say what you need, in your README or your
registry entry, in the form the picker uses: a backend and a level. "Needs
`viewer` on a MikroTik backend to answer questions about firewall rules" is a
sentence someone can act on in about ten seconds.
