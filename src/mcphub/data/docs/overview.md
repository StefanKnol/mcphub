# Building for mcphub

mcphub is a self-hosted platform that puts MCP servers behind one sign-in. An
app attaches to it in one or two ways, and they are independent:

- **As an MCP server.** The hub proxies it, and it becomes its own connector
  with its own OAuth client registration, its own token, and its own tool list.
- **As a web interface.** The hub serves it at `/ui/<slug>`, behind the same
  sign-in and the same grants as the MCP endpoint.

Most apps do both. A dictionary, say: tools for an assistant to look words up
and propose new ones, and a page for a person to browse the same data.

## What you get without writing it

- **Authentication.** OAuth 2.1 with dynamic client registration and PKCE. Your
  server needs none of its own; point the hub at it on a private address and
  firewall its port.
- **Accounts and grants.** Backends are shared and granted out per account.
  Checked on every request, so revoking access cuts off a live connector.
- **Levels.** Each grant carries `viewer`, `user` or `admin`, enforced by the
  hub from your tool annotations. See `annotations`.
- **Storage.** A directory of your own that survives restarts. See `storage`.
- **Version pinning.** If you publish to PyPI or npm and the MCP registry,
  accounts can pin a version and the hub runs each one that is in use.

## What you still own

- What your tools do, and saying so honestly in their annotations. The hub
  enforces levels from what you declare; declaring nothing is a choice too.
- Anything your web interface decides on the strength of the identity headers.
  The hub can enforce a level over MCP, where tools say what they do. Over HTTP
  a POST is just a POST.
- Your own data. The hub gives you a directory; it does not migrate, lock or
  back up what you put in it beyond backing up the volume as a whole.

## The shape of a backend

One configured backend is one mount. Two routers wrapped through the proxy are
two backends sharing a plugin, each at its own `/mcp/<slug>`, each registered in
a client as its own connector. Tool lists are deliberately not merged: a single
wrapped server can be over a hundred tools, and a hub with several would spend
a great deal of context before anything is asked.

## Where to go next

| Topic | |
| --- | --- |
| `annotations` | Mark what your tools do, or levels cannot work |
| `web-interface` | Serving your UI through the hub |
| `storage` | The directory you get, and who shares it |
| `levels` | What viewer, user and admin mean |
| `plugin` | Writing a Python plugin instead of being proxied |
| `publishing` | Getting into the MCP registry so the hub can find you |
| `deploying` | Attaching a finished app to a hub |
