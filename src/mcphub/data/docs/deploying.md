# Deploying an app to a hub

Three ways in, from least to most involved.

## 1. From the registry

**Add from registry**, search, pick it. The hub reads the server's registry
entry, builds a settings form with a field per declared environment variable,
and launches it with `uvx` or `npx` when the backend is enabled.

Backends added this way are created **disabled**. Their tool surface comes from
outside the hub, and a newly added third-party server should not attach its
tools to an account before anyone has looked at what they are. Open it, read the
tool list, tick what you want exposed, then enable it.

## 2. A server you run yourself

Add an **MCP server (proxy)** backend and give it the URL of your server's
streamable-HTTP endpoint, including the path. Then firewall that port to the
hub — otherwise the authentication is decorative, because the original open port
is still there.

Use this for an app in its own container. A compose service on the same network
as the hub needs no published port at all:

```yaml
services:
  dictionary:
    image: ghcr.io/you/dictionary:latest
    volumes:
      - /srv/mcphub/apps/dictionary:/data      # see `storage`
    networks: [hub]
    # no ports: — the hub reaches it by service name

  mcphub:
    image: ghcr.io/you/mcphub:latest
    environment:
      MCPHUB_PUBLIC_URL: https://mcp.example.com
    volumes:
      - /srv/mcphub:/data
    ports: ["8080:8080"]
    networks: [hub]
```

The backend's URL is then `http://dictionary:8000/mcp`.

## 3. As a plugin

Install your plugin package into the hub's environment and it appears in the
list of backend kinds with its own settings form. See `plugin`.

In a container, that means your own image:

```dockerfile
FROM ghcr.io/you/mcphub:latest
RUN pip install --no-cache-dir dictionary-mcphub-plugin
```

## Then, for any of them

1. **Add the web interface**, if you have one. Set its address, decide sandboxed
   or trusted, and use **Check UI** before believing it works. See
   `web-interface`.
2. **Mount storage** if your app is in its own container. The path is on the
   backend's settings page. See `storage`.
3. **Grant it**, with a level per account. See `levels`.
4. **Register the connector.** Each backend is its own endpoint at
   `<public url>/mcp/<slug>` and is added to a client as its own connector, so
   tool lists stay separate.

## Connecting a client

Add `https://your-hub/mcp/<slug>` as a custom connector. The client discovers
the authorization server, registers itself, and sends you to the hub's sign-in.
The token it gets is bound to that one backend — replayed against another
backend on the same hub, it is refused.

If a connector authenticates successfully and then every request fails with
`421 Misdirected Request`, `MCPHUB_PUBLIC_URL` does not match the name the hub
is answering on. That is DNS-rebinding protection doing its job, and it looks
exactly like an authentication fault without being one.
