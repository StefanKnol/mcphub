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

Then point a proxy host at it with a real certificate. The first-run admin
password is printed once, in the container log.

### Configuration

Only these are environment variables. Everything else lives in the database.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCPHUB_PUBLIC_URL` | `http://localhost:8080` | Externally reachable origin. Must be HTTPS in production. |
| `MCPHUB_DATA_DIR` | `/data` | Holds `hub.db` and `master.key`. |
| `MCPHUB_HOST` / `MCPHUB_PORT` | `0.0.0.0` / `8080` | Bind address. |
| `MCPHUB_DEV` | unset | Permits a non-HTTPS public URL. Local development only. |

Back up `/data`. Losing `master.key` makes every stored backend credential
permanently unreadable.

## The MikroTik plugin

Talks to RouterOS over the **binary API** (8729 with TLS, or 8728 plaintext),
not by driving the CLI over SSH.

That choice is the point. Scraping `/ip firewall filter print` gives you the
CLI's *positional* numbers, which are not the rules' identities. The server
this replaces listed rules by position and then looked them up for writing with
`where .id=<position>` — a predicate that can never match — so every write
failed on rules that existed. Had it matched, it would have been worse:
positions shift when rules are added, removed, or when a dynamic rule appears,
so the write would have hit a different rule than the caller meant.

The binary API returns the real `.id` (`*7`, `*1f`) on every read. So:

- every read returns `id`, and every write takes one back;
- list results also carry `position`, which is display-only and **refused** for
  writes, with an error that says why;
- writes read back what they wrote, so a change can be verified rather than
  assumed;
- `remove_firewall_rule` takes an optional `confirm_comment` and returns the
  rule it deleted.

Enable the service on the router and use a dedicated account, not `admin`:

```
/ip service enable api-ssl
/user group add name=mcp policy=api,read,write,test,policy
/user add name=mcp-agent group=mcp password=<strong-password>
```

Set the TLS fingerprint in the backend's settings if you can. MikroTik's
API-SSL certificate is self-signed, so ordinary CA validation cannot succeed
against a stock device; pinning is what makes the connection authenticated
rather than merely encrypted.

`ros_list` reads any RouterOS path, which covers everything without a dedicated
tool. There is deliberately no generic *write* escape hatch.

## The proxy plugin

Wraps an MCP server you already run. Point it at an endpoint and that server
inherits the hub's authentication without a line of its own code changing.

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

Built and tested end to end: the hub, the OAuth server, the config UI, the
MikroTik plugin (24 tools) and the proxy plugin. Per-backend token isolation
and the tool allowlist are verified by test, not assumed — including that a
filtered-out tool cannot be called.

The proxy is exercised against a real Unraid Management Agent: 126 tools
discovered, narrowed to 5, schemas preserved, live calls forwarded.

Known limits:

- Only tools are proxied. The Unraid agent also exposes 6 prompts and 5
  resources; those are not forwarded yet.
- Non-text tool results (images, embedded resources) are described rather than
  passed through.
- Schema synthesis is exact for flat object schemas. A deeply nested upstream
  schema would degrade, and the tool says so in its description when it does.
