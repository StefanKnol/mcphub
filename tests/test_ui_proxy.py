"""Serving a backend's own web interface through the hub.

The reason to proxy rather than publish another hostname: a second name is
another surface that can expose /mcp by accident, and it would need its own
login — at which point the hub has stopped earning its place.

The reason it must be sandboxed is concrete rather than theoretical. Served on
the hub's own origin, a proxied page's JavaScript can call `/accounts` with the
admin session attached and read the response. That was demonstrated, not
assumed: with the header removed the request returned 200 and the page put the
result in its own title.
"""

import pytest

from mcphub.web.uiproxy import (
    SANDBOX,
    _rewrite_root_absolute,
    STRIP_REQUEST,
    STRIP_RESPONSE,
    _inject_base,
    is_proxyable,
)


def test_the_sandbox_withholds_same_origin_access():
    """`allow-same-origin` is the whole difference between a sandboxed page and
    one that can act as the signed-in administrator."""
    assert "sandbox" in SANDBOX
    assert "allow-same-origin" not in SANDBOX


def test_the_sandbox_still_lets_an_interface_work():
    for flag in ("allow-scripts", "allow-forms"):
        assert flag in SANDBOX


@pytest.mark.parametrize("url,ok", [
    ("http://10.0.0.50:8080", True),
    ("https://ui.example/app", True),
    ("ftp://host/x", False),
    ("not a url", False),
    ("", False),
])
def test_only_http_urls_are_proxyable(url, ok):
    assert is_proxyable(url) is ok


# ── what is not forwarded ─────────────────────────────────────────────────

def test_the_hubs_session_cookie_never_reaches_the_upstream():
    assert "cookie" in STRIP_REQUEST


def test_the_upstream_cannot_set_a_cookie_on_the_hubs_origin():
    assert "set-cookie" in STRIP_RESPONSE


def test_the_upstream_cannot_replace_the_sandbox():
    """Its own CSP is dropped, or it could relax the one thing holding this up."""
    assert "content-security-policy" in STRIP_RESPONSE


def test_the_upstreams_framing_policy_is_not_relayed():
    assert "x-frame-options" in STRIP_RESPONSE


def test_hop_by_hop_headers_are_not_forwarded():
    for header in ("connection", "transfer-encoding", "upgrade"):
        assert header in STRIP_REQUEST and header in STRIP_RESPONSE


# ── making a root-written interface work under a mount ────────────────────

def test_a_base_is_injected_into_head():
    out = _inject_base(b"<html><head><title>x</title></head>", "/ui/aenvae/")
    assert b'<base href="/ui/aenvae/">' in out
    assert out.index(b"<base") < out.index(b"<title")


def test_a_document_without_a_head_still_gets_one():
    assert b'<base href="/ui/x/">' in _inject_base(b"<html><body>hi</body></html>", "/ui/x/")


def test_an_existing_base_is_respected():
    """An interface that already knows where it lives is not second-guessed."""
    original = b'<html><head><base href="/somewhere/"></head>'
    assert _inject_base(original, "/ui/x/") == original


def test_injection_is_case_insensitive():
    assert b"<base" in _inject_base(b"<HTML><HEAD></HEAD>", "/ui/x/")


def test_nosniff_is_not_added_to_proxied_responses():
    """The page is in an opaque origin, so every asset is a cross-origin
    request. `nosniff` then lets Cross-Origin Read Blocking refuse anything
    whose declared type does not match its use — which turns a merely
    mislabelled stylesheet into a hard block, for content whose types we do
    not control."""
    import inspect

    from mcphub.web import uiproxy

    source = inspect.getsource(uiproxy.forward)
    assert "nosniff" not in source.replace("# ", "").split("Deliberately")[0]


def test_the_upstreams_content_type_is_forwarded():
    """CORB decides by declared type, so the upstream's own must survive."""
    assert "content-type" not in STRIP_RESPONSE


# ── root-absolute references ──────────────────────────────────────────────
# A <base> only affects *relative* references. A root-absolute one leaves the
# mount, lands on the hub's own 404, and comes back as HTML — which the browser
# then refuses with Cross-Origin Read Blocking, naming the stylesheet rather
# than the cause. This is the fix for that, and it is the likelier of the two
# reasons a proxied interface loses its styling.

@pytest.mark.parametrize("markup,expected", [
    (b'<link href="/styles.css">', b'<link href="/ui/a/styles.css">'),
    (b"<script src='/js/app.js'>", b"<script src='/ui/a/js/app.js'>"),
    (b"<img src=/logo.png>", b"<img src=/ui/a/logo.png>"),
    (b'<form action="/search">', b'<form action="/ui/a/search">'),
])
def test_root_absolute_references_are_moved_under_the_mount(markup, expected):
    assert _rewrite_root_absolute(markup, "/ui/a/") == expected


@pytest.mark.parametrize("markup", [
    b'<script src="//cdn.example.com/x.js">',   # protocol-relative: another origin
    b'<a href="https://elsewhere.example/x">',  # absolute: not ours
    b'<a href="relative.html">',                # the <base> handles this
    b'<a href="./also-relative">',
])
def test_other_references_are_left_alone(markup):
    assert _rewrite_root_absolute(markup, "/ui/a/") == markup


def test_rewriting_does_not_touch_arbitrary_slashes():
    """Only URL-bearing attributes, so prose and data survive."""
    body = b'<p>use /etc/hosts</p><div data-note="/not/a/url">'
    assert _rewrite_root_absolute(body, "/ui/a/") == body


# ── the compatibility check ───────────────────────────────────────────────
# Written because the browser's own error is unhelpful: an asset answered with
# a page is refused as CORB, and the message names the asset rather than the
# path that missed.

import httpx2  # noqa: E402

from mcphub.web.uiproxy import check  # noqa: E402


def serving_ui(handler):
    return httpx2.MockTransport(handler)


async def test_an_interface_with_nothing_wrong_says_so():
    page = b'<html><head><link href="styles.css"></head></html>'

    def handler(request):
        if request.url.path.endswith("styles.css"):
            return httpx2.Response(200, content=b"h1{}", headers={"content-type": "text/css"})
        return httpx2.Response(200, content=page, headers={"content-type": "text/html"})

    result = await check("http://ui.test", "/ui/a/", serving_ui(handler))
    assert result["ok"] and "nothing found" in result["detail"]


async def test_an_asset_answered_with_a_page_is_named():
    """The CORB case, reported as the asset and what came back instead."""
    page = b'<html><head><link href="/app.css"></head></html>'
    result = await check("http://ui.test", "/ui/a/", serving_ui(
        lambda r: httpx2.Response(200, content=page, headers={"content-type": "text/html"})))
    assert "CORB" in result["detail"]
    assert "/app.css" in result["detail"]


async def test_an_interface_that_sets_a_cookie_is_flagged():
    """Its own login cannot work in an origin that has no cookies."""
    result = await check("http://ui.test", "/ui/a/", serving_ui(
        lambda r: httpx2.Response(200, content=b"<html></html>",
                                  headers={"content-type": "text/html", "set-cookie": "s=1"})))
    assert "cookie" in result["detail"]


async def test_an_unreachable_interface_reports_failure():
    def handler(request):
        raise httpx2.ConnectError("refused")

    assert (await check("http://ui.test", "/ui/a/", serving_ui(handler)))["ok"] is False


# ── sandboxed or trusted ──────────────────────────────────────────────────

async def test_a_sandboxed_response_carries_the_sandbox():
    import httpx2 as _h
    from starlette.requests import Request as _R

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
             "query_string": b"", "path_params": {"path": ""}}
    request = _R(scope, receive=lambda: {"type": "http.request", "body": b""})
    # forward() builds its own client, so this asserts on the decision rather
    # than the network: the flag is what selects the header.
    from mcphub.web import uiproxy

    assert "sandbox" in uiproxy.SANDBOX
    source = __import__("inspect").getsource(uiproxy.forward)
    assert "if trusted:" in source
    assert 'out.pop("content-security-policy", None)' in source


def test_identity_headers_are_only_for_trusted_apps():
    """A sandboxed page could not act on an identity, and the account name is
    not owed to an app nobody vouched for."""
    import inspect

    from mcphub.web import routes

    source = inspect.getsource(routes.build)
    assert "x-mcphub-user" in source
    assert "if trusted else None" in source
