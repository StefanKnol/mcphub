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
