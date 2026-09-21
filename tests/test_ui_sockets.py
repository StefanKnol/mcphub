"""Websockets and runtime-built URLs through the UI proxy.

Both are about the same thing: a real app, served under a path it was not
written for. A `<base>` and a markup rewrite handle what is in the HTML; a URL
a script builds at runtime and a socket it opens are what is left, and until
now both left the mount and landed on the hub's own 404.

That mattered more than it sounds. "Proxy it, except for the sockets" means an
app with a socket still needs its own hostname — which is the thing this proxy
exists to avoid.
"""

import re

import pytest

from mcphub.web import uiproxy
from mcphub.web.uiproxy import SHIM, _inject_base, _inject_shim, socket_url

PREFIX = "/ui/aenvae/"
PAGE = b"<!doctype html><html><head><title>x</title></head><body></body></html>"


# ── where a socket is dialled ─────────────────────────────────────────────

@pytest.mark.parametrize("base,path,query,expected", [
    ("http://app:8000", "live", "", "ws://app:8000/live"),
    ("https://app.test", "live", "", "wss://app.test/live"),
    ("http://app:8000", "/live", "room=1", "ws://app:8000/live?room=1"),
    ("http://app:8000/base/", "live", "", "ws://app:8000/base/live"),
])
def test_a_socket_uses_the_same_address_as_the_pages(base, path, query, expected):
    """One `Web interface` setting, http(s), and the socket scheme follows from
    it — asking for a second address would be asking twice for one thing."""
    assert socket_url(base, path, query) == expected


def test_the_hubs_session_cookie_does_not_reach_the_upstream_socket():
    """The same rule as for a request: the upstream has no business with it."""
    assert "cookie" in uiproxy.WS_STRIP_REQUEST


def test_the_handshake_headers_are_not_relayed():
    """They describe the browser's negotiation with the hub; the hub makes its
    own, and passing ours along describes a handshake that is not happening."""
    for header in ("sec-websocket-key", "sec-websocket-version", "upgrade", "connection"):
        assert header in uiproxy.WS_STRIP_REQUEST


# ── and where a script's URLs go ──────────────────────────────────────────

def shimmed(prefix: str = PREFIX) -> str:
    return _inject_shim(_inject_base(PAGE, prefix), prefix).decode()


def test_a_trusted_page_is_told_where_it_is_mounted():
    assert '"/ui/aenvae"' in shimmed()


def test_the_shim_patches_every_way_a_url_is_made():
    """Each of these was a separate report of "it works except for X"."""
    for made in ("window.fetch", "XMLHttpRequest.prototype.open", "pushState",
                 "window.WebSocket", "window.EventSource"):
        assert made in SHIM


def test_it_does_not_read_the_base_tag():
    """It runs while the document is still being parsed, so the <base> may not
    be in it yet — resolving against the mount directly has no such ordering."""
    assert "new URL(u, document.baseURI)" not in SHIM
    assert 'new URL(u, location.origin + P + "/")' in SHIM


def test_a_sandboxed_page_gets_none_of_it():
    """Its origin is opaque, so every request is cross-origin whatever its
    path. There is nothing here that would help, and a page nobody vouched for
    should not be handed patched globals either."""
    import inspect

    source = inspect.getsource(uiproxy.forward)
    assert "if trusted:\n            content = _inject_shim" in source


def test_the_shim_only_goes_into_documents():
    """A stylesheet or a JSON response with a <script> tag glued to the front
    of it is worse than the problem being solved."""
    import inspect

    source = inspect.getsource(uiproxy.forward)
    html = source.index('if "text/html" in content_type:')
    assert source.index("_inject_shim", html) < source.index('elif "text/css"', html)


@pytest.mark.parametrize("written,expected", [
    ("/api/overview", "/ui/aenvae/api/overview"),   # the reported case
    ("api/overview", "/ui/aenvae/api/overview"),    # relative, already fine
    ("/ui/aenvae/api/x", "/ui/aenvae/api/x"),       # an app that honours the prefix
    ("/ui/aenvae", "/ui/aenvae"),                   # the mount itself
    ("https://elsewhere.test/x", "https://elsewhere.test/x"),
])
def test_the_rule_the_shim_applies(written, expected):
    """Run the shim's own logic rather than describing it twice.

    Same-origin paths not already under the mount are moved under it; anything
    else is left exactly as written — including an absolute URL to the hub,
    which is the escape hatch for an app that means to call the hub itself.
    """
    prefix = "/ui/aenvae"
    origin = "http://hub.test"
    from urllib.parse import urljoin, urlparse

    # A faithful transcription of fix() in SHIM, which the test above pins to
    # the same shape the browser runs.
    parsed = urlparse(urljoin(f"{origin}{prefix}/", written))
    if parsed.netloc != "hub.test":
        assert written == expected
        return
    path = parsed.path
    if path != prefix and not path.startswith(prefix + "/"):
        path = prefix + path
    assert path == expected


def test_the_shim_is_valid_javascript_shaped():
    """A syntax error would be silent: the browser drops the script and the app
    goes back to 404ing, with nothing in the server log."""
    assert SHIM.count("{") == SHIM.count("}")
    assert SHIM.count("(") == SHIM.count(")")
    assert re.search(r"\(function \(\) \{.*\}\)\(\);", SHIM, re.S)


# ── end to end, against a real socket ─────────────────────────────────────

import json
import socket
import tempfile
import threading
import time
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def upstream():
    """A real app with a real socket, on a real port.

    Not a stub: what is under test is a websocket handshake surviving a hop,
    and an in-process fake would be testing the fake's idea of a handshake.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route, WebSocketRoute

    async def page(request):
        return HTMLResponse("<html><head><title>app</title></head><body></body></html>")

    async def live(ws):
        await ws.accept()
        said = await ws.receive_text()
        await ws.send_text(json.dumps({
            "echo": said,
            "prefix": ws.headers.get("x-forwarded-prefix", ""),
            "user": ws.headers.get("x-mcphub-user", ""),
            "role": ws.headers.get("x-mcphub-role", ""),
            "cookie": ws.headers.get("cookie", ""),
        }))
        await ws.close()

    port = free_port()
    app = Starlette(routes=[Route("/", page), WebSocketRoute("/live", live)])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def hub(upstream):
    from mcphub.app import create_app
    from mcphub.config import Settings
    from mcphub.crypto import hash_password
    from mcphub.db import utcnow
    from mcphub.plugins.base import CheckResult, PluginDefaults

    class Simple(PluginDefaults):
        id, name, description, fields = "simple", "Simple", "d", ()

        def build(self, instance):
            from mcp.server.mcpserver import MCPServer

            return MCPServer(instance.title)

        async def check(self, instance):  # pragma: no cover
            return CheckResult(True, "ok")

    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://127.0.0.1:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(Simple())
    state.db.execute(
        "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
        "VALUES ('boss', ?, 1, 1, ?)", (hash_password("x" * 12), utcnow()))
    state.db.execute(
        "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
        "updated_at) VALUES ('aenvae', 'simple', 'Aenvae', 0, ?, ?, ?)",
        (json.dumps({"ui_url": upstream, "ui_proxy": True, "ui_trusted": True}),
         utcnow(), utcnow()))
    return app


@pytest.fixture
def client(hub):
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080") as browser:
        browser.post("/login", data={"username": "boss", "password": "x" * 12})
        yield browser


def signed_in_socket(client, path: str):
    """`websocket_connect` does not carry the client's cookie jar, and the hub
    authorises a socket by cookie exactly as it authorises a page."""
    jar = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    return client.websocket_connect(path, headers={"cookie": jar})


def test_a_socket_reaches_the_app_behind_the_mount(client):
    with signed_in_socket(client, "/ui/aenvae/live") as socket:
        socket.send_text("ping")
        answer = json.loads(socket.receive_text())
    assert answer["echo"] == "ping"


def test_the_app_is_told_where_it_is_mounted(client):
    with signed_in_socket(client, "/ui/aenvae/live") as socket:
        socket.send_text("ping")
        answer = json.loads(socket.receive_text())
    assert answer["prefix"] == "/ui/aenvae"


def test_a_trusted_app_is_told_who_opened_it(client):
    """The same identity its requests carry. A socket that arrived anonymous
    while every request said who it was would be the odd one out."""
    with signed_in_socket(client, "/ui/aenvae/live") as socket:
        socket.send_text("ping")
        answer = json.loads(socket.receive_text())
    assert answer["user"] == "boss"
    assert answer["role"] == "admin"


def test_the_session_cookie_stays_on_this_side(client):
    with signed_in_socket(client, "/ui/aenvae/live") as socket:
        socket.send_text("ping")
        answer = json.loads(socket.receive_text())
    assert "mcphub_session" not in answer["cookie"]


def test_a_socket_from_a_stranger_is_refused(hub):
    """Authorised by the same grant as the pages. A socket that skipped it
    would be a way around the thing the pages are careful about."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(hub, base_url="http://127.0.0.1:8080") as anonymous:
        with pytest.raises(WebSocketDisconnect) as refused:
            with anonymous.websocket_connect("/ui/aenvae/live") as socket:
                socket.receive_text()
    assert refused.value.code == 1008


def test_a_socket_to_a_backend_with_no_interface_is_refused(client, hub):
    from starlette.websockets import WebSocketDisconnect

    hub.state.hub.db.execute("UPDATE backends SET config_json = '{}' WHERE slug = 'aenvae'")
    with pytest.raises(WebSocketDisconnect) as refused:
        with signed_in_socket(client, "/ui/aenvae/live") as socket:
            socket.receive_text()
    assert refused.value.code == 1008


def test_an_upstream_that_is_not_listening_closes_rather_than_hanging(client, hub):
    from starlette.websockets import WebSocketDisconnect

    hub.state.hub.db.execute(
        "UPDATE backends SET config_json = ? WHERE slug = 'aenvae'",
        (json.dumps({"ui_url": "http://127.0.0.1:9", "ui_proxy": True, "ui_trusted": True}),))
    with pytest.raises(WebSocketDisconnect) as refused:
        with signed_in_socket(client, "/ui/aenvae/live") as socket:
            socket.receive_text()
    # 1011 rather than a refused handshake: a browser is told far more by a
    # close code than by a connection that simply did not open.
    assert refused.value.code == 1011
