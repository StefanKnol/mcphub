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


async def forward(request: Request, upstream_base: str, prefix: str, *,
                  trusted: bool = False, identity: dict[str, str] | None = None) -> Response:
    """Pass one request to the upstream UI and return its response.

    `trusted` decides the one thing that matters here. A sandboxed interface
    gets an origin of its own, which is what stops its scripts acting as the
    signed-in administrator — but it also makes every asset a cross-origin
    request with no cookies, and an ES module (always fetched in CORS mode) or
    anything using `fetch` will not load at all. There is no header that fixes
    that; it is what an opaque origin means.

    So an app the administrator vouches for is served on the hub's own origin
    instead, and is sent the signed-in account so it can use these accounts
    rather than keeping its own. That is a real grant of authority, made
    deliberately, rather than a default that quietly breaks things.
    """
    path = request.path_params.get("path", "")
    target = urljoin(upstream_base.rstrip("/") + "/", path.lstrip("/"))
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}
    # Identify the mount, so an upstream that can use it builds correct links.
    headers["x-forwarded-prefix"] = prefix.rstrip("/")
    for key, value in (identity or {}).items():
        headers[key] = value

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

    if trusted:
        # No sandbox: the app runs on this origin, which is what lets its
        # modules, cookies and CORS requests work at all. It can also reach
        # every endpoint here as the signed-in account, which is the trade the
        # administrator made when ticking the box.
        out.pop("content-security-policy", None)
    else:
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


ASSET_REF = re.compile(
    rb"""\s(?:src|href)\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)

EXPECTED_TYPE = {
    ".css": "text/css",
    ".js": "javascript",
    ".mjs": "javascript",
    ".json": "json",
    ".png": "image/", ".jpg": "image/", ".jpeg": "image/",
    ".svg": "image/", ".gif": "image/", ".webp": "image/", ".ico": "image",
    ".woff": "font", ".woff2": "font",
}


async def check(upstream_base: str, prefix: str, transport: object = None) -> dict[str, object]:
    """Fetch an interface and report what would stop it working through here.

    Written because the browser's own error is unhelpful: an asset answered
    with the hub's 404 page is refused by Cross-Origin Read Blocking, and the
    message names the stylesheet rather than the path that missed. This says
    which asset, and what came back instead.
    """
    findings: list[str] = []
    try:
        # `transport` is a seam for tests; production passes nothing.
        options = {"transport": transport} if transport is not None else {}
        async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=True, **options) as client:
            page = await client.get(upstream_base)
            body, page_type = page.content, page.headers.get("content-type", "")

            if page.status_code != 200:
                return {"ok": False,
                        "detail": f"{upstream_base} answered {page.status_code}."}

            if "set-cookie" in {k.lower() for k in page.headers}:
                findings.append(
                    "it sets a cookie — the sandbox gives it an origin of its own, so "
                    "cookies will not persist and any login of its own will not work")

            refs = [r.decode("utf-8", "replace") for r in ASSET_REF.findall(body[:200_000])]
            local = [r for r in refs if not r.startswith(("http://", "https://", "//", "#", "data:", "mailto:"))]
            root_absolute = [r for r in local if r.startswith("/")]
            if root_absolute:
                findings.append(
                    f"{len(root_absolute)} asset reference(s) start at the root and are "
                    "rewritten under the mount; anything a script builds at runtime is not, "
                    "so honour the X-Forwarded-Prefix header for those")

            # The case that actually bites: an asset that answers with a page.
            broken: list[str] = []
            for ref in local[:12]:
                suffix = "." + ref.rsplit(".", 1)[-1].split("?")[0].lower() if "." in ref else ""
                expected = EXPECTED_TYPE.get(suffix)
                if not expected:
                    continue
                try:
                    asset = await client.get(urljoin(upstream_base.rstrip("/") + "/", ref.lstrip("/")))
                except httpx2.HTTPError:
                    broken.append(f"{ref} (unreachable)")
                    continue
                got = asset.headers.get("content-type", "")
                if asset.status_code != 200 or expected not in got:
                    broken.append(f"{ref} -> {asset.status_code} {got or 'no type'}")
            if broken:
                findings.append(
                    "these answer with something other than what they claim to be, which the "
                    "browser refuses as CORB: " + "; ".join(broken[:4]))

    except httpx2.ConnectError as exc:
        return {"ok": False, "detail": f"Could not reach {upstream_base}: {exc}"}
    except httpx2.HTTPError as exc:
        return {"ok": False, "detail": f"{upstream_base} failed: {exc}"}

    if not findings:
        return {"ok": True,
                "detail": f"Reachable, and nothing found that would stop it working at {prefix}."}
    return {"ok": True, "detail": "Reachable, with caveats — " + "; ".join(findings)}
