"""A plugin's own browser interface, served in-process behind the hub's gate.

`Plugin.web_app` lets a plugin bring its own ASGI app instead of an address the
hub proxies. The hub builds it once at mount time, beside the MCP server, from
the same instance (storage path included), and the `/ui/{slug}` endpoint hands
requests to it after the same session and grant checks every proxied interface
gets — with who is asking on the scope, since the app cannot read the hub's
session itself.
"""

import json
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcphub.plugins.base import WEB_IDENTITY_KEY, CheckResult, PluginDefaults


def make_shelf_plugin(seen: dict):
    """A plugin with an interface that reports what the hub told it."""
    from mcp.server.mcpserver import MCPServer

    class Shelf(PluginDefaults):
        id, name, description, fields = "shelf", "Shelf", "a shelf", ()

        def uses_storage(self, instance):
            return True

        def build(self, instance):
            seen["build_storage"] = instance.storage
            return MCPServer(instance.title)

        def web_app(self, instance):
            seen["web_app_storage"] = instance.storage

            async def report(request):
                return JSONResponse({
                    "identity": request.scope.get(WEB_IDENTITY_KEY),
                    "root_path": request.scope.get("root_path"),
                    "path": request.scope.get("path"),
                    "route_path": request.path_params.get("rest", ""),
                    "query": str(request.query_params),
                    "method": request.method,
                })

            return Starlette(routes=[
                Route("/", report, methods=["GET", "POST"]),
                Route("/{rest:path}", report, methods=["GET", "POST"]),
            ])

        async def check(self, instance):  # pragma: no cover
            return CheckResult(True, "ok")

    return Shelf()


class Broken(PluginDefaults):
    """An interface that fails to build must not take the tools down with it."""

    id, name, description, fields = "broken", "Broken", "b", ()

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(instance.title)

    def web_app(self, instance):
        raise RuntimeError("no interface today")

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")


@pytest.fixture
def seen():
    return {}


@pytest.fixture
def hub(seen):
    from mcphub.app import create_app
    from mcphub.config import Settings
    from mcphub.crypto import hash_password
    from mcphub.db import utcnow

    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://127.0.0.1:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    state.registry.register(make_shelf_plugin(seen))
    state.registry.register(Broken())
    for username, admin in (("boss", 1), ("guest", 0)):
        state.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, ?, 0, ?)", (username, hash_password("x" * 12), admin, utcnow()))
    for slug, plugin_id in (("shelf", "shelf"), ("broken", "broken")):
        state.db.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
            "updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (slug, plugin_id, slug.title(), json.dumps({}), utcnow(), utcnow()))
    return app


@pytest.fixture
def browser(hub):
    """A TestClient inside the lifespan, so the enabled backends are mounted."""
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080") as client:
        yield client


def sign_in(browser, username="boss"):
    browser.post("/login", data={"username": username, "password": "x" * 12})


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_navigation_without_a_session_is_sent_to_login(browser):
    response = browser.get("/ui/shelf/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/ui/shelf/")


def test_a_script_request_without_a_session_gets_a_401_it_can_read(browser):
    """A fetch that followed the redirect would land on the login page's HTML
    with a 200; an editor could not tell that from a saved file."""
    response = browser.post("/ui/shelf/v/notes/a.md", headers={"sec-fetch-mode": "cors"},
                            follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"error": "session_expired", "login": "/login"}
    assert response.headers["cache-control"] == "no-store"


def test_a_navigation_marked_as_such_still_redirects(browser):
    response = browser.get("/ui/shelf/", headers={"sec-fetch-mode": "navigate"}, follow_redirects=False)
    assert response.status_code == 303


def test_an_account_without_a_grant_is_refused(browser):
    sign_in(browser, "guest")
    response = browser.get("/ui/shelf/")
    assert response.status_code == 403
    assert "not been granted" in response.text


# ── the hand-over ─────────────────────────────────────────────────────────────

def test_the_app_is_told_who_is_asking(browser):
    from mcphub.crypto import hash_token

    sign_in(browser)
    response = browser.get("/ui/shelf/")
    assert response.status_code == 200
    identity = response.json()["identity"]
    session_cookie = browser.cookies.get("mcphub_session")
    assert identity == {
        "user": "boss",
        "admin": True,
        "role": "admin",
        "prefix": "/ui/shelf",
        "csrf_token": hash_token(f"csrf:{session_cookie}"),
        "public_url": "http://127.0.0.1:8080",
    }


def test_the_app_sees_its_own_paths(browser):
    """Mounted the way Mount does it: the full path stays, root_path grows, so
    the app routes by what follows the mount and builds links that resolve."""
    sign_in(browser)
    response = browser.get("/ui/shelf/v/notes/a%20b.md?line=3")
    body = response.json()
    assert body["root_path"] == "/ui/shelf"
    assert body["path"] == "/ui/shelf/v/notes/a b.md"
    assert body["route_path"] == "v/notes/a b.md"
    assert body["query"] == "line=3"


def test_a_post_reaches_the_app_too(browser):
    sign_in(browser)
    assert browser.post("/ui/shelf/save").json()["method"] == "POST"


def test_the_app_is_built_once_with_the_same_instance_as_the_server(browser, seen):
    sign_in(browser)
    browser.get("/ui/shelf/")
    assert seen["web_app_storage"] is not None
    assert seen["web_app_storage"] == seen["build_storage"]


def test_the_bare_mount_still_redirects_to_its_slash(browser):
    sign_in(browser)
    response = browser.get("/ui/shelf", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/ui/shelf/"


# ── when there is no app ─────────────────────────────────────────────────────

def test_a_failing_interface_leaves_the_tools_up(browser, hub):
    """The interface is optional; the endpoint is not."""
    sign_in(browser)
    mounts = hub.state.hub.mounts
    assert "broken" in {mounted.slug for mounted in mounts.active()}
    assert mounts.web_app_for("broken") is None
    response = browser.get("/ui/broken/")
    assert response.status_code == 404
    assert "no web interface configured" in response.text


def test_a_plugin_without_an_interface_answers_none(browser):
    assert PluginDefaults().web_app(None) is None


def test_the_hook_is_an_optional_attribute_the_hub_validates():
    from mcphub.plugins.base import OPTIONAL_ATTRIBUTES, validate_plugin

    assert "web_app" in OPTIONAL_ATTRIBUTES
    validate_plugin(Broken())


# ── the shared stylesheet ────────────────────────────────────────────────────

def test_the_stylesheet_is_served_and_cacheable(browser):
    from mcphub.web.routes import HUB_STYLESHEET_VERSION

    plain = browser.get("/static/hub.css")
    assert plain.status_code == 200
    assert plain.headers["content-type"].startswith("text/css")
    assert plain.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert "--accent" in plain.text

    versioned = browser.get(f"/static/hub.css?v={HUB_STYLESHEET_VERSION}")
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"

    again = browser.get("/static/hub.css", headers={"if-none-match": plain.headers["etag"]})
    assert again.status_code == 304


def test_every_hub_page_links_the_stylesheet(browser):
    from mcphub.web.routes import HUB_STYLESHEET_VERSION

    assert f'href="/static/hub.css?v={HUB_STYLESHEET_VERSION}"' in browser.get("/login").text
