# Serving your web interface through the hub

Set **Web interface** on a backend to your app's address and the hub serves it
at `/ui/<slug>`, behind its own sign-in and the same grant as the MCP endpoint.
An app with no login of its own gets one, and you do not publish another
hostname that could expose something by accident.

There are two modes, and the difference is not cosmetic.

## Sandboxed (the default)

The page is served with a `Content-Security-Policy: sandbox` that withholds
`allow-same-origin`. It gets an opaque origin of its own, which is what stops
its JavaScript calling the hub's own endpoints as the signed-in administrator.
That was demonstrated rather than assumed: without it, a proxied page's
`fetch('/accounts', {credentials:'include'})` returns 200.

The cost is real and you will hit it:

1. **No cookies.** An interface with its own login cannot work here. One with
   no login loses nothing.
2. **Every asset is cross-origin.** An ES module — always fetched in CORS mode —
   will not load, and no header fixes it. That is what an opaque origin means.
3. **`fetch` from your own page** to your own backend is cross-origin too.

If your app is a server-rendered page with plain `<script>` tags and relative
asset paths, sandboxed works and is the safer choice.

## Trusted

Tick **This is an app I control** and the page is served on the hub's own
origin instead. Modules load, `fetch` works, cookies apply.

So does everything else on that origin. A trusted app's JavaScript can call the
hub's endpoints as whoever is signed in. Tick it for an app you wrote or would
trust with your administrator session, and not otherwise.

A trusted app is also told who is asking:

| Header | |
| --- | --- |
| `X-Mcphub-User` | the account name |
| `X-Mcphub-Admin` | `1` or `0` |
| `X-Mcphub-Role` | `viewer`, `user` or `admin` on this backend |
| `X-Forwarded-Prefix` | where the app is mounted, e.g. `/ui/dictionary` |

Which means your app does not need accounts at all: the hub authenticates,
checks the grant, and tells you who it is talking to. A sandboxed app is sent
none of this — it could not act on it anyway.

`X-Mcphub-Role` is reported, not enforced. Over MCP the hub knows what a tool
does; over HTTP it sees a method and a path. What a level means inside your app
is yours to decide — but decide it, or the level stops at the connector.

## Four things a proxied interface has to do

The **Check UI** button on the backend's card tests these against your running
interface rather than leaving you to find out from a browser error.

1. **No login of its own** (sandboxed mode only — see above).
2. **Serve assets as what they are.** A stylesheet answered with `text/html` is
   refused as CORB, and the browser's error names the stylesheet rather than the
   cause. An app that answers unknown paths with its index page produces exactly
   this.
3. **Relative asset paths, or honour `X-Forwarded-Prefix`.** The hub injects a
   `<base>` and rewrites root-absolute references in markup and CSS. URLs your
   JavaScript builds at runtime are beyond both, so read the header.
4. **No WebSockets.** They are not proxied.

## Being reachable at all

The hub fetches your interface server-side, so it needs an address the hub's
container can reach — a service name on the same Docker network, a LAN address,
anything routable from there. It does not have to be reachable by the browser,
and it is better if it is not.
