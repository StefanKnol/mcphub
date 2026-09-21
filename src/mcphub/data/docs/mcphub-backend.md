# The mcphub backend

Every hub has one backend it did not get from anyone: `mcphub`, at
`/mcp/mcphub`. It is built in — the hub creates it, keeps the name reserved, and
refuses to delete it — but in every other respect it is an ordinary mount, with
the same OAuth, grants and levels as anything else. An administrator who does
not want it exposed can disable it; disabling is reversible and deleting is not.

It carries two things: this documentation, and tools for managing the hub.

## Documentation

| Tool | |
| --- | --- |
| `list_topics` | Every page, in reading order, with a one-line summary |
| `read_topic` | One page in full, as Markdown |
| `search_docs` | Lines across every page that mention something |

Each page is also a resource at `docs://<name>`, so a client can attach one
directly rather than calling a tool for it.

All three are read-only, so they work at every level including `viewer`.

## A brief for making an app

There is also one MCP prompt, `make_it_an_mcphub_app`: a short brief to hand an
assistant working in an app's own repository, which walks it through an MCP
server, tool annotations, storage, the identity headers, app grants and
shipping — mostly as pointers back into the pages above, so it stays in step
with the build rather than repeating it.

It takes an optional `app` argument naming what to work on. The same text is on
the hub's **Make an app** page with a copy button, for pasting somewhere that
has no connector to this hub.

## Managing the hub

| Tool | Level | |
| --- | --- | --- |
| `list_backends` | viewer | What is on this hub that you may see |
| `describe_backend` | viewer | One backend in full, minus its secrets |
| `search_registry` | viewer | Find a server in the official MCP registry |
| `deploy_app` | user | Add a backend, from the registry or a URL |
| `set_backend_enabled` | user | Bring an endpoint up or take it down |
| `remove_backend` | admin | Delete a backend, its credentials and its grants |

Two checks apply to every one of these, and they are not the same check:

- **Hub rights.** Whether the account may configure backends at all, exactly as
  the settings pages ask. Without them you can read and nothing more.
- **The level** on this backend, enforced by the hub from the annotations above.

Both, because they answer different questions: one is about the account, the
other about what this connector was granted. Neither substitutes for the other,
so granting someone `admin` on `mcphub` does not make them able to configure
backends, and being able to configure backends does not let a `viewer`
connector deploy anything.

## Deploying, end to end

```
search_registry("mikrotik")
    → io.github.StefanKnol/mikrotik-mcp, declares MIKROTIK_HOST, MIKROTIK_USER, ...

deploy_app(slug="router",
           registry_name="io.github.StefanKnol/mikrotik-mcp",
           environment={"MIKROTIK_HOST": "10.0.0.1", ...})
    → created disabled, with the tools it found

describe_backend("router")
    → read the tool list, check nothing surprising arrived

set_backend_enabled("router", true)
    → live at https://your-hub/mcp/router
```

A deployed backend arrives **disabled** on purpose. Its tools come from outside
this hub, and they should be looked at before they attach to an account. That is
the same rule the **Add from registry** page follows, for the same reason.

What is deliberately not here: granting accounts, changing levels, and creating
people. Those are decisions about who may do what, and they are made by a person
on a page, not by a connector holding a token.
