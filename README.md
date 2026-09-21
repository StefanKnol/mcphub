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

**Real OAuth.** A full OAuth 2.1 authorization server with PKCE,
refresh-token rotation, and protected resource metadata (RFC 9728). Clients may
either register dynamically (RFC 7591) or present a **client ID metadata
document** — an HTTPS URL describing the client, which the hub fetches instead
of requiring registration. Claude uses the latter, so there is nothing to set
up on either side. Each backend is a distinct RFC 8707 *resource*, so a token
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

### Updating

Three separate things update, and they are not the same thing:

**The hub itself**, including the proxy plugin, updates with the image:

```bash
docker compose pull && docker compose up -d
```

**A launched server's code.** `uvx` and `npx` resolve their package again each
time they start, so restarting the backend is what picks up a new release —
press **Update** on its card. That relaunches it and re-reads what it offers,
reporting what actually changed:

```
updated 1.29.0 -> 1.30.0; 1 new tool(s): convert_time
```

**Per-account version pinning.** Anyone granted a backend can choose which
version *they* get, from the selector on its card. It is not a permission and
it affects nobody else — which means the hub runs both versions at once, each
started the first time someone on it connects, and a version nobody uses costs
nothing.

Each version gets its own server rather than a swapped subprocess, because a
version can offer a different set of tools: of one real server, 0.14.8.0 has
174 and 0.15.0.0 has 182. Offering an account a tool its own version lacks
would fail only when it tried to call it.

A version is launched and read when it is first pinned, so one that cannot
start is refused there and then, with the reason, rather than at the next
connection. Only registry-added backends can be pinned — guessing which token
of a hand-written command is the package would eventually rewrite the wrong
one. For those, pin in the command itself:

```
uvx mikrotik-mcp==0.1.0
npx -y some-server@1.4.2
```

**Knowing an update exists.** A background check asks the registry hourly
whether a newer version of each registry-backed backend has been published, and
marks the card when one has. It changes nothing on its own. The registry serves
repeats from its own cache and publishes no rate limit, so one small query per
backend per hour is unremarkable; failures back off, and startup is staggered so
restarted hubs do not arrive in lockstep.

**The tool list this hub serves.** Cached when the backend is saved, so that an
endpoint still mounts when its upstream is down. **Update** re-reads it. Until
you do, a server that gained tools keeps being advertised with the old list —
so if an upstream released something and you cannot see it, that button is why.
Tools the upstream no longer has are dropped from the allowlist at the same
time, rather than lingering and quietly reappearing if it ever brings them back.

### Client ID metadata documents

A client may present an HTTPS URL as its `client_id` rather than registering.
The hub fetches that URL, reads the client metadata from it, and proceeds —
which is how a client connects to a server it has never met.

The security shape is the inverse of registration, and worth being explicit
about: an **unauthenticated** caller hands the hub a URL and the hub makes an
outbound request to it. That is a request-forgery primitive unless it is
fenced, so:

- HTTPS only, and the URL must have a path — a bare origin is refused.
- Every resolved address must be public unicast. Private, loopback and
  link-local addresses are refused, because this hub usually sits on a LAN with
  a router on it and a `client_id` must not become a way to reach it.
- Redirects are not followed at all, since a public URL redirecting to a
  private one is the ordinary way past an address check.
- The body is capped at 64 KB, the timeout is short, and both successes and
  failures are cached so a `client_id` is at most one request per interval.
- A document claiming a different `client_id` than the URL it came from is
  refused, and a `client_secret` in a document is discarded — a document cannot
  confer a secret on itself, so these are public clients and PKCE carries the
  weight.
- A URL-shaped `client_id` cannot be registered over, or whoever registered
  first would own that identity.

Set `MCPHUB_CIMD=0` to turn it off, at the cost of only working with clients
that register.

### Accounts

The first run creates one administrator. Further accounts are added under
**Accounts**, each carrying two toggles and a set of grants:

| | |
| --- | --- |
| Administrator | Manages accounts, and reaches every backend without a grant. |
| May configure backends | Add, edit and remove backends. They are shared, so this affects everyone granted them. |
| Grants | Which backends this account may use. |

Backends are shared rather than per-account: configured once, then granted out,
so a router's password lives in one place and there is one page showing who can
reach it.

The grant is checked **on every request**, not when the token was issued, so
removing access cuts off an existing connector at once rather than whenever its
token happens to expire. Authorising a connector for a backend an account has
not been granted is refused at sign-in, with the reason, instead of succeeding
and then failing on use.

### Configuration

Only these are environment variables. Everything else lives in the database.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCPHUB_PUBLIC_URL` | `http://localhost:8080` | Externally reachable origin. Must be HTTPS in production. |
| `MCPHUB_DATA_DIR` | `/data` | Holds `hub.db` and `master.key`. |
| `MCPHUB_HOST` / `MCPHUB_PORT` | `0.0.0.0` / `8080` | Bind address. |
| `MCPHUB_CIMD` | `1` | Accept a `client_id` that is an HTTPS URL describing the client. Set `0` to require registration instead. |
| `MCPHUB_UPDATE_INTERVAL` | `3600` | Seconds between registry update checks. `0` disables them. |
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

A plugin supplies `id`, `name`, `description`, `fields`, `build()` and
`check()`, mixes in `PluginDefaults` for the rest, and is advertised on the
`mcphub.plugins` entry point group. The built-in proxy plugin uses exactly this
path — there is no privileged route into the registry.

```python
from mcphub.plugins.base import (
    BackendInstance, CheckResult, ConfigField, PluginDefaults,
)
from mcp.server.mcpserver import MCPServer

class UnraidPlugin(PluginDefaults):
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
has to mount, so its own tools can report the failure. A plugin that fails to
import is logged and skipped rather than taking the hub down with it.

`PluginDefaults` answers the hooks the hub calls on every plugin — `fields_for`,
`options`, `on_save`, `variant`, `tool_names` and `review_before_enable`. They
are optional to *write*, not optional to *have*: a plugin supplying none of them
is refused at load rather than raising later inside a request, with the settings
page half drawn. Override the ones you want:

| hook | what it buys you |
|---|---|
| `fields_for(instance)` | a form shaped by *this* backend rather than one generic form |
| `options(instance, key)` | the choices for a `multiselect`, fetched live when the form renders |
| `validate(instance)` | refuse a configuration that cannot work, before it is saved |
| `on_save(instance)` | cache what you discovered, so `build()` can stay offline |
| `variant(instance, version)` | the same backend as it runs at a pinned version |
| `on_delete(instance)` | release what this backend holds elsewhere, before it is forgotten |
| `tool_names(instance)` | lets Update report *what* changed, not just that something did |
| `review_before_enable` | create backends of this kind disabled, pending a look at their tools |

### Refusing a configuration

The form enforces what `fields` declares — required, numeric — and nothing more.
`validate()` is where a plugin says the rest: that a port is out of range, that a
URL needs a scheme, that two boxes contradict each other. Return a `FieldError`
and the message lands under that box; return a bare string and it goes in the
banner at the top, which is where something true of the whole form belongs.

```python
def validate(self, instance: BackendInstance) -> list[FieldError]:
    problems = []
    if instance.get("auth_value") and not instance.get("auth_header"):
        problems.append(FieldError(
            "Name the header to send this value in, or it will not be sent "
            "at all.", "auth_header"))
    return problems
```

It runs on every save, after the declared constraints are satisfied and before
anything is written — so it may assume required fields are present, and it is
the last word on whether the backend is coherent. Returning problems refuses the
save; a validator that *raises* also refuses it, with the exception shown on the
form. Failing closed is the only safe direction for a validator: swallowing the
error the way `on_save` does would save precisely the configuration the plugin
meant to stop.

`validate()` is about the values; `check()` is about whether they work. Keep the
two apart. `validate()` is synchronous and does no I/O — no network, no
subprocess, no clock — because it is on the save path and because a backend
whose device is merely switched off still has to be savable. `check()` is the
one that goes and looks, on demand behind the Test button, and may take as long
as it takes.

Test sits on the settings page as well as the dashboard, and the two ask
different questions. The dashboard asks about the backend *as saved*. The
settings page posts the form and asks about what is *typed* — so `check()` sees
the instance a save would store, including a withheld secret left blank to keep
its stored value, without anything being written. `validate()` runs first, so a
plugin never has to diagnose a configuration it already knows is incoherent.
On a backend that does not exist yet there is nothing saved to fall back on,
which is where this is worth the most.

`on_delete()` is the mirror of `on_save()` and runs once the endpoint is down,
with the configuration still intact, so a plugin can undo what it provisioned
while it still holds the credentials to do it with. It is the one hook that
fails *open*: a failure is reported on the dashboard and the removal goes ahead,
because a deletion the user already asked for is not the plugin's to veto. The
hub releases the backend's own OAuth tokens and authorization codes at the same
time, and its grants and pins cascade with the row.

### Shaping a longer form

`group` files consecutive fields under a heading, and the heading disappears
when every field beneath it is conditioned away, so a form can be long without
being a wall.

`show_if` takes one value or several — `show_if=("mode", ("url", "proxy"))`
shows the field for either — and conditions **chain**, so a branch can have
sub-branches: a field whose controller is itself hidden is hidden too. On a
checkbox the value is `"true"` or `"false"`; a checkbox carries no value of its
own, so anything else can never match. A condition may point at any field
holding a single value, text boxes included — the page watches `input` as well
as `change`, so it keeps up with typing.

`choices_from_plugin=True` fills a `select` from `options()` when the form is
drawn, for choices only knowable then — the interfaces a router actually has,
the databases a server actually holds. A `multiselect` always works this way
and needs no flag.

### Values with no field left to show them

A backend keeps whatever was saved for it, and an upstream that drops a
variable from its declaration leaves the value behind. For a launched server
that orphan is not inert: it is still put in the environment on every start,
through a box the form no longer draws. The settings page lists them under
*Stored, but no longer asked for*, each with a checkbox to let it go. They are
shown rather than pruned — deleting one on the quiet would change what the
server receives exactly as silently as keeping it does.

### What the form will reject

`validate_plugin` runs at load and refuses a `fields` declaration that would
render wrongly rather than let it through to a form that merely looks fine. It
reports every problem at once, so one pass fixes the lot. It refuses a key that
is duplicated, unusable as an HTML control name, one of the form's own names
(`plugin_id`, `title`, `slug`, `enabled`), or one that shadows the clear-value
checkbox of another field (`clear_x` beside `x` — a value typed there would
delete `x`'s stored secret). It refuses an unknown `type`, a `select` with no
`choices`, and a `default` that is outside those choices or the wrong type for
the field. And it refuses a `show_if` that names a field you did not declare,
points at itself, points at a field that is itself conditional, or points at
anything other than a `select` or a checkbox — those are the only controls the
page watches for changes, so a condition on a text box is read once at page load
and then never again.

`secret=True` stores a field encrypted, in the sealed blob rather than in the
plaintext config. Whether it comes *back* when the form reopens is the separate
`show_value`, which defaults to following `secret`:

```python
ConfigField("api_key", "API key", type="password", secret=True)
ConfigField("host", "Host", secret=True, show_value=True)
```

The first reopens as an empty box marked with dots to say something is saved;
leaving it blank keeps the stored value, and an optional one gets a checkbox to
clear it. The second is encrypted at rest but shown back, which is right for a
value worth keeping out of a database dump but not worth hiding from the
administrator who typed it — a router's address beside its password. A field
that is shown back has no "blank means unchanged" rule: it renders with its
value in it, so an emptied box is an instruction to empty it.

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
