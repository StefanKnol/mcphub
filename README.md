# mcphub

A self-hosted MCP platform. Backends are plugins; each configured backend gets
its own OAuth-protected MCP endpoint; everything is configured in a web UI
instead of environment variables.

Built to replace a setup with three specific problems: an MCP server exposed to
the internet with no authentication at all, firewall write tools that reported
"not found" for rules that plainly existed, and configuration that lived in env
vars and a YAML file.

## What it does

**One endpoint per backend.** A configured backend is mounted at
`/mcp/<slug>` and added to Claude as its own connector. Backends are never
merged into a single endpoint — the MikroTik plugin alone exposes 24 tools and
the server this replaces exposed 182, so merging burns context before you ask
anything and measurably degrades tool selection.

**Real OAuth.** A full OAuth 2.1 authorization server with dynamic client
registration (RFC 7591), PKCE, refresh-token rotation, and protected resource
metadata (RFC 9728). Each backend is a distinct RFC 8707 *resource*, so a token
issued for your router is rejected if replayed against another backend on the
same hub.

**Configuration in a browser.** Add a device, rotate a password, disable a
backend — it takes effect immediately, with no restart and no YAML. Credentials
are encrypted at rest and never sent back to the browser.

## Running it

```bash
docker compose up -d
```

Set `MCPHUB_PUBLIC_URL` to the URL clients actually reach — your reverse
proxy's, not the container's. OAuth discovery compares issuer strings exactly,
so a mismatch here breaks connection rather than degrading it.

On first start the log prints a generated `admin` password, once:

```
====================================================================
First run: created the 'admin' account.
  username: admin
  password: ...
====================================================================
```

Sign in, change it, add a backend, then paste the endpoint URL shown on the
dashboard into Claude as a custom connector. Claude registers itself, you sign
in, and you approve the connection.

### Deploying

Every push to `main` runs the test suite and, if it passes, builds a
multi-arch image (amd64 + arm64) and pushes it to your container registry.
Tags matching `v*` also publish semver tags. Pull requests build the image to
prove the Dockerfile still works, but do not push.

Repository secrets drive it — the registry host included, so nothing about
your infrastructure lives in the workflow:

| Secret | Required | Value |
| --- | --- | --- |
| `DOCKER_REGISTRY` | yes | Registry host, e.g. `registry.example.com`. |
| `DOCKER_USERNAME` | yes | Registry account. |
| `DOCKER_PASSWORD` | yes | Registry password or access token. Prefer a token where the registry supports one. |
| `DOCKER_IMAGE` | no | Full image path when the registry needs a namespace, e.g. `registry.example.com/homelab/mcphub`. Defaults to `<DOCKER_REGISTRY>/mcphub`. |

```bash
gh secret set DOCKER_REGISTRY
gh secret set DOCKER_USERNAME
gh secret set DOCKER_PASSWORD
```

Two things the runner needs, both easy to miss:

- **The registry must be reachable from the public internet.** GitHub-hosted
  runners cannot see a LAN-only registry. If yours is internal, use a
  self-hosted runner on that network instead.
- **Its TLS certificate must chain to a public CA.** A private CA or a
  self-signed certificate will fail the login on a hosted runner.

#### On Unraid

Add a container with the published image and:

| Setting | Value |
| --- | --- |
| Repository | `<user>/mcphub:latest` |
| Port | `8080` → `8080` |
| Path | `/mnt/user/appdata/mcphub` → `/data` |
| Variable | `MCPHUB_PUBLIC_URL` = the URL your reverse proxy serves |
| Variable | `PUID` = `99`, `PGID` = `100` (defaults; Unraid's `nobody:users`) |

The container starts as root only long enough to align `/data` with
`PUID`/`PGID`, then drops to that user before running anything. Without that
step a bind mount owned by someone else fails as
`sqlite3.OperationalError: unable to open database file`, which says nothing
about ownership. Pass `--user` to skip it and manage the ownership yourself.

Then point a proxy host at it with a real certificate. The first-run admin
password is printed once, in the container log.

### Configuration

Only these are environment variables. Everything else lives in the database.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCPHUB_PUBLIC_URL` | `http://localhost:8080` | Externally reachable origin. Must be HTTPS in production. |
| `MCPHUB_DATA_DIR` | `/data` | Holds `hub.db` and `master.key`. |
| `MCPHUB_HOST` / `MCPHUB_PORT` | `0.0.0.0` / `8080` | Bind address. |
| `MCPHUB_ALLOWED_HOSTS` | derived | Extra Host header values to accept, comma-separated. Only needed when the hub answers on a name other than `MCPHUB_PUBLIC_URL`. |
| `MCPHUB_DEV` | unset | Starlette debug output. Does not relax the HTTPS requirement — OAuth needs an HTTPS issuer, so only `localhost` and `127.0.0.1` may use http. |

The MCP transport enforces DNS-rebinding protection, accepting only Host
headers matching `MCPHUB_PUBLIC_URL` (plus loopback). Get that variable wrong
and requests fail with `421 Misdirected Request` *after* a completely
successful sign-in, which looks like an authentication fault and is not one.

Back up `/data`. Losing `master.key` makes every stored backend credential
permanently unreadable.

## MikroTik

MikroTik lives in its own package now:
**[mikrotik-mcp](https://github.com/StefanKnol/mikrotik-mcp)**. It is a
standalone MCP server, so it works with any client, not only this hub.

Add it here as a launched server — a proxy backend with the command:

```
uvx mikrotik-mcp
```

and `MIKROTIK_HOST`, `MIKROTIK_USERNAME`, `MIKROTIK_PASSWORD` in the
Environment field, where they are encrypted at rest. It then runs in its own
process and cannot read credentials held for other backends.

That package also ships an `mcphub.plugins` entry point, so it can be loaded
in-process with typed settings fields instead, if you install it into the hub's
environment and accept that an in-process plugin sees everything the hub holds.

> Upgrading from a build where MikroTik was bundled: an existing `mikrotik`
> backend will report its plugin as missing and stay unmounted. Re-create it as
> a launched server with the command above; nothing else changes.

## The proxy plugin

Wraps an MCP server — one you already run, or one the hub launches for you.
Either way it inherits the hub's authentication without a line of its own code
changing.

### Installing servers from npm and PyPI

Give the backend a command instead of a URL and the hub runs the server itself:

```
npx -y @modelcontextprotocol/server-filesystem /data
uvx mcp-server-time --local-timezone=Europe/Amsterdam
```

npm and PyPI are the plugin registry, so there is nothing to host and nothing
to install by hand. The image ships Node and uv for exactly this; downloads are
cached on the data volume, so a restart does not refetch them. Servers that
need an API key take one through the **Environment** field (`KEY=VALUE` per
line), which is encrypted at rest like any other secret.

A launched server runs in **its own process**. It cannot read the credentials
stored for other backends, cannot reach the OAuth tables, and cannot touch the
encryption key — none of which is true of a plugin loaded into the hub. That is
the reason to prefer this route for third-party code, and the reason the hub
does not install Python plugins from PyPI at runtime.

New proxy backends are created **disabled**. Adding one installs and
introspects the server but mounts nothing, so you see its tool list and choose
what to expose before anything attaches to your account.

That is how Unraid is handled: the Unraid Management Agent already speaks MCP
on `http://<server>:8043/mcp`, so there is nothing to reimplement — it just
needs fronting.

Wrapping buys three things the upstream cannot do for itself:

**Authentication.** The agent answers `initialize` with no credentials at all
and sends `Access-Control-Allow-Origin: *`. Behind the hub it gets OAuth,
dynamic client registration and a token scoped to that backend alone.

**A tool allowlist.** The agent exposes 126 tools — about 10,000 tokens just to
list them, spent before you ask anything. The settings page fetches the live
tool list and lets you tick the ones you want; the rest are not registered, so
they are genuinely uncallable rather than merely hidden.

**Several views of one server.** Point two backends at the same upstream with
different selections — a read-only `unraid-status` and an `unraid-admin` — and
each is its own connector with its own token.

Upstream tool schemas are preserved: the plugin synthesises a Python signature
that reproduces the upstream JSON Schema, so descriptions, enums, required
fields and `destructiveHint` all survive the hop. That round trip is exact for
flat object schemas, which is every tool the Unraid agent exposes. Anything it
cannot represent is named in the tool's own description rather than dropped
quietly.

Tool schemas are cached when you save, so the endpoint still mounts when the
upstream is down — its tools then report the failure themselves.

> Once an upstream is wrapped, firewall its own port to the hub. Otherwise the
> authentication is decorative: the original open port is still there.

## Adding servers from the registry

The dashboard can search the
[official MCP registry](https://registry.modelcontextprotocol.io). A published
server ships a `server.json` declaring how it runs and what it needs, so both
the command line and a correctly typed settings form are generated rather than
typed — a value the server marked secret gets a password field, and everything
it declares is encrypted at rest whether or not it was flagged.

Search, pick, fill in the settings, and it is added **disabled**; you land on
its settings page to review its tools and choose which to expose.

Servers not in the registry are still added by hand, with a command or a URL.

### Verified servers

A result can carry one of two badges, and the difference matters.

**Launches** means the server started, completed a handshake and listed its
tools. That is a liveness check and nothing more. The MikroTik server this
project was built to replace passes it comfortably — it starts fine and lists
182 tools fine, and every one of its write tools is broken. A badge that
stopped here would be measuring the wrong thing confidently.

**✓ Verified** means that, plus every behavioural probe the entry declares ran
and returned what it should. A probe is a read-only tool call with an expected
outcome, declared in `verified.json`:

```json
{"tool": "update_firewall_rule", "arguments": {"rule_id": "3"},
 "expectError": "position",
 "why": "a positional index is refused before it can reach the device"}
```

That one exercises the exact logic the replaced server got wrong, and needs no
router to do it: probes run against TEST-NET addresses, so anything that would
reach a real device simply fails to connect.

Neither badge is a claim by the server's author, and neither says every tool
works or that a server is safe to run. A probe says the behaviour it names is
the behaviour observed. Everything without a badge is unchecked, not suspect.

`src/mcphub/data/verified.json` holds the results and ships with the hub, so the
badge reflects the build you are running. `.github/workflows/verify-servers.yml`
re-runs the checks weekly and opens a pull request when what it observes
changes; a server that quietly stops launching turns the build red rather than
keeping its badge. The script self-tests its own harness first, so "everything
failed" is distinguishable from "the harness is broken".

## Writing a plugin

A plugin is any object with `id`, `name`, `description`, `fields`, `build()`
and `check()`, advertised on the `mcphub.plugins` entry point group. The
built-in MikroTik plugin uses exactly this path — there is no privileged route
into the registry.

```python
from mcphub.plugins.base import BackendInstance, CheckResult, ConfigField
from mcp.server.mcpserver import MCPServer

class UnraidPlugin:
    id = "unraid"
    name = "Unraid"
    description = "Manage an Unraid server."
    fields = (
        ConfigField("host", "Host", placeholder="192.168.1.50"),
        ConfigField("api_key", "API key", type="password", secret=True),
    )

    def build(self, instance: BackendInstance) -> MCPServer:
        mcp = MCPServer(name=f"unraid-{instance.slug}", title=instance.title)

        @mcp.tool(name="list_containers")
        async def list_containers(ctx) -> str:
            ...
        return mcp

    async def check(self, instance: BackendInstance) -> CheckResult:
        return CheckResult(True, "Connected")

PLUGIN = UnraidPlugin()
```

```toml
[project.entry-points."mcphub.plugins"]
unraid = "your_package:PLUGIN"
```

`build()` must not do network I/O — a backend that is merely unreachable still
has to mount, so its own tools can report the failure. `fields` marked `secret`
are encrypted at rest and never rendered back into a form. A plugin that fails
to import is logged and skipped rather than taking the hub down with it.

Two rules worth keeping in your own tools:

- **Raise `mcp.server.mcpserver.exceptions.ToolError`** for anticipated
  failures. Any other exception is treated as a crash and the model sees only
  `Error executing tool <name>` — your message is discarded.
- **Never accept a positional index as a write handle.** It is the bug this
  project was built to stop repeating.

## Development

```bash
uv sync --extra dev
uv run pytest
MCPHUB_DEV=1 MCPHUB_DATA_DIR=./data MCPHUB_PUBLIC_URL=http://127.0.0.1:8080 uv run python -m mcphub
```

## Status

Built and tested end to end: the hub, the OAuth server, the config UI and the
proxy plugin, over both transports. Per-backend token isolation and the tool
allowlist are verified by test, not assumed — including that a filtered-out
tool cannot be called.

The proxy is exercised against a real Unraid Management Agent over HTTP (126
tools discovered, narrowed to 5, schemas preserved, live calls forwarded) and
against a published PyPI server launched over stdio.

The hub ships one backend of its own, the proxy. Everything else is a package:
see [mikrotik-mcp](https://github.com/StefanKnol/mikrotik-mcp).

Tools and resources are proxied, including each tool's `_meta`, so an
upstream using [MCP Apps](https://modelcontextprotocol.io) keeps its interface
through the hop: the `ui://` resource its tool points at is exposed and read
through on demand, with its `text/html;profile=mcp-app` type and `_meta.ui`
sandbox policy intact.

Prompts are proxied too, keeping the names, descriptions and required flags
they were published with.

Known limits:

- Non-text tool results (images, embedded resources) are described rather than
  passed through.
- Schema synthesis is exact for flat object schemas. A deeply nested upstream
  schema would degrade, and the tool says so in its description when it does.
