Make this project work as an app on mcphub — a self-hosted platform that puts
MCP servers behind one sign-in, at {hub}.

**First, read the platform's own documentation.** It ships with the hub and
describes the build actually running, so prefer it over anything you remember
about mcphub. If the `mcphub` connector is available, call `list_topics` and
then `read_topic` for whatever below is relevant. Do not guess at header names
or environment variables — look them up.

Work through this in order, and skip anything the project already does or will
never need. Say which you skipped and why.

**1. An MCP server.** The project needs one, over streamable HTTP or stdio.
Wrap what the project already does; do not invent capabilities for it.

**2. Annotate every tool.** This is the one that matters most, because the hub
enforces per-account levels from these and nothing else:

- `readOnlyHint: true` — changes nothing.
- `destructiveHint: true` — can destroy or overwrite something a person would
  mind losing. Deleting is destructive; adding is not.

A tool with no annotations is withheld from a viewer and allowed for a user, so
a server that annotates nothing offers a viewer nothing. Be accurate rather
than generous in either direction. (`read_topic("annotations")`)

**3. A data directory, if it keeps anything.** The hub can give the backend one
and hand over the path as `MCPHUB_STORAGE`, or as whatever variable name is
configured for it. Read that variable if it is set and fall back to the
project's own default if it is not — it must still run outside a hub.
(`read_topic("storage")`)

**4. The web interface, if there is one.** It gets served at `/ui/<slug>` on the
hub's own hostname, so:

- Relative asset paths, or honour the `X-Forwarded-Prefix` header, which names
  the mount. Root-absolute paths in markup and CSS are rewritten for you, and a
  trusted app also gets its runtime `fetch`/`XMLHttpRequest`/`WebSocket` URLs
  rewritten — but honouring the header is the thing that works everywhere.
- Serve assets with their real content types. Answering an unknown path with
  the index page gets the asset refused as CORB, and the browser's error names
  the asset rather than the cause.
- Websockets are proxied through the same mount; derive their address from the
  page's own origin rather than hard-coding one.
  (`read_topic("web-interface")`)

**5. Use the hub's accounts instead of your own.** A trusted app is sent
`X-Mcphub-User`, `X-Mcphub-Admin` and `X-Mcphub-Role` (`viewer`, `user` or
`admin`) on every request. If the project has a login of its own, make it
optional and prefer these when present. The role is reported, not enforced —
decide what each level may do inside the app, or the level stops at the
connector. (`read_topic("levels")`)

**6. Calling other backends, if it needs to.** Credentials for whatever the app
was granted arrive as `MCPHUB_BACKENDS` in the environment, or the
`X-Mcphub-Backends` header — one entry per backend, each with a `url`, a
`token` and the `level` that token will actually get. They rotate, so read them
at start-up and re-read rather than treating a 401 as fatal.
(`read_topic("app-access")`)

**7. Ship it.** Publish to PyPI or npm and add a `server.json` so the hub can
find and launch it from the registry, or build a container and point a backend
at its URL. Declare every environment variable with a description — that text
becomes the help under the box on the settings form.
(`read_topic("publishing")`, `read_topic("deploying")`)

Finally, write a short README section saying how to add this to an mcphub: what
to set, what the tools are, and which of them are destructive.

Do not change what the project does. Adding an MCP surface to it is the task.
