"""Serve a backend's own web interface through the hub.

A backend that is more than an MCP server — one that also has a web UI — can
have that UI served at `/ui/{slug}`, behind the same sign-in and the same
per-account grants as its MCP endpoint. The point is not convenience: it puts
an interface that has no login of its own behind one, and saves publishing
another hostname to reach it.

The interface runs in an **opaque origin**, forced by a `Content-Security-Policy:
sandbox` header on every response. Without that, the proxied page would share
mcphub's origin, and its JavaScript could issue same-origin requests that carry
the admin session — `POST /accounts/2` and it has granted itself everything.
`SameSite` does not help there, because such a request is not cross-site.

The cost of an opaque origin is that the upstream's own cookies stop working,
so a UI with its own login will not authenticate through this. That is the
trade, and it is the right way round for the case this exists to serve.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx2
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

log = logging.getLogger(__name__)

TIMEOUT = 30.0
MAX_BYTES = 25 * 1024 * 1024

SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals"
"""Deliberately without `allow-same-origin`: that one flag is the difference
between a sandboxed page and a page that can act as the signed-in admin."""

# Hop-by-hop headers are about one connection and must not be forwarded.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

# Never forwarded to the upstream: the hub's session cookie is not its business,
# and the host must be the upstream's own.
STRIP_REQUEST = HOP_BY_HOP | {"cookie", "host", "authorization", "content-length"}

# Never returned to the browser: an opaque origin cannot use cookies anyway, and
# the upstream's framing and transport policies are not ours to relay.
STRIP_RESPONSE = HOP_BY_HOP | {
    "set-cookie", "content-length", "content-encoding", "content-security-policy",
    "content-security-policy-report-only", "x-frame-options",
    "strict-transport-security", "public-key-pins",
}

BASE_TAG = re.compile(rb"<head[^>]*>", re.IGNORECASE)

# Root-absolute references in markup: src="/x", href='/x', action=/x. Not
# protocol-relative (`//host/x`), which is a different origin and not ours to
# rewrite.
ROOT_ABSOLUTE = re.compile(
    rb"""(\s(?:src|href|action|poster|data-src)\s*=\s*)(["']?)/(?!/)""",
    re.IGNORECASE,
)


def is_proxyable(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _rewrite_root_absolute(body: bytes, prefix: str) -> bytes:
    """Point `/styles.css` at `/ui/{slug}/styles.css`.

    A `<base>` cannot do this: it only affects *relative* references. A
    root-absolute one leaves the mount entirely, lands on the hub's own 404,
    and comes back as HTML — which the browser then refuses with Cross-Origin
    Read Blocking, naming the stylesheet rather than the cause. So the markup
    is rewritten.

    Only attributes in markup. URLs a script builds at runtime are beyond this,
    and need the upstream to honour the `X-Forwarded-Prefix` it is sent.
    """
    return ROOT_ABSOLUTE.sub(rb"\1\2" + prefix.rstrip("/").encode() + b"/", body)


def _inject_base(body: bytes, prefix: str) -> bytes:
    """Point relative URLs at the mount, by adding a <base> to the document.

    A UI written to live at the root asks for `styles.css`, which under a mount
    would resolve against `/ui/`, not `/ui/{slug}/`.
    """
    if b"<base" in body[:4096].lower():
        return body
    tag = f'<base href="{prefix}">'.encode()
    match = BASE_TAG.search(body)
    if match:
        return body[: match.end()] + tag + body[match.end():]
    return tag + body


async def forward(request: Request, upstream_base: str, prefix: str) -> Response:
    """Pass one request to the upstream UI and return its response."""
    path = request.path_params.get("path", "")
    target = urljoin(upstream_base.rstrip("/") + "/", path.lstrip("/"))
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}
    # Identify the mount, so an upstream that can use it builds correct links.
    headers["x-forwarded-prefix"] = prefix.rstrip("/")

    body = await request.body()
    if len(body) > MAX_BYTES:
        return PlainTextResponse("Request too large.", status_code=413)

    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            upstream = await client.request(
                request.method, target, headers=headers,
                content=body or None,
            )
    except httpx2.ConnectError as exc:
        log.info("ui proxy: could not reach %s: %s", target, exc)
        return PlainTextResponse(
            f"This backend's interface could not be reached at {upstream_base}.", status_code=502)
    except httpx2.HTTPError as exc:
        log.info("ui proxy: %s failed: %s", target, exc)
        return PlainTextResponse("The backend's interface returned an error.", status_code=502)

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in STRIP_RESPONSE}
    content = upstream.content

    content_type = upstream.headers.get("content-type", "")
    if "text/html" in content_type:
        content = _rewrite_root_absolute(content, prefix)
        content = _inject_base(content, prefix)
    elif "text/css" in content_type:
        # url(/x) inside a stylesheet has the same problem as src="/x".
        content = re.sub(rb"""(url\(\s*["']?)/(?!/)""",
                         rb"\1" + prefix.rstrip("/").encode() + b"/", content)
    elif path and "." in path.rsplit("/", 1)[-1]:
        # An asset request answered with a document almost always means the
        # upstream did not recognise the path — CORB will refuse it, and the
        # error names the asset rather than the cause, so say so here.
        log.info("ui proxy: %s returned %r for %s; if that is an asset, the path "
                 "is not reaching the upstream", upstream_base, content_type or "no type", path)

    location = upstream.headers.get("location")
    if location:
        # Keep a redirect inside the mount; otherwise it escapes to the
        # upstream's own address, which the browser may not even be able to reach.
        parsed = urlparse(location)
        if not parsed.scheme and location.startswith("/"):
            out["location"] = prefix.rstrip("/") + location

    out["content-security-policy"] = SANDBOX
    # Deliberately *not* adding `nosniff`. The sandbox puts this page in an
    # opaque origin, so every asset it asks for is a cross-origin request, and
    # Cross-Origin Read Blocking then refuses any response whose declared type
    # does not match how it is being used. Adding `nosniff` to content whose
    # types we do not control turns a merely mislabelled stylesheet into a hard
    # CORB block. The upstream's own type is forwarded untouched and the browser
    # decides.
    #
    # CORB will still refuse an HTML response used as a stylesheet or a script,
    # which is the case worth knowing about: an app that answers unknown paths
    # with its index page produces exactly that, and it means the asset path is
    # wrong rather than the type.
    # This page is not the hub, and nothing it does should be cached as if it were.
    out.setdefault("cache-control", "no-store")

    return Response(content=content, status_code=upstream.status_code, headers=out)
