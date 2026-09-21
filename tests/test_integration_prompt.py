"""The brief for making an app work on this hub.

Documentation answers "how does this work"; a prompt answers "do this to my
project", which is a different thing to hand someone. It goes out two ways —
as an MCP prompt on the hub's own backend, and as text on a page with a copy
button — because the person holding the app is often not the thing that will do
the work.

It is deliberately mostly pointers. What these check is that the pointers point
at things that exist: a prompt naming a page that was renamed sends an assistant
looking for documentation the hub does not have, and nothing says so.
"""

import re
import tempfile
from pathlib import Path

import httpx2 as httpx
import pytest

from mcphub.app import create_app
from mcphub.config import Settings
from mcphub.crypto import hash_password
from mcphub.db import utcnow
from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.hub import SLUG, topics
from mcphub.plugins.builtin.hub.prompts import NAME, PROMPTS_DIR, integration_prompt

HUB_URL = "https://mcp.example.com"
PASSWORD = "correct horse battery"


@pytest.fixture
def hub():
    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url=HUB_URL,
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    app.state.hub.db.execute(
        "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
        "VALUES ('boss', ?, 1, 1, ?)", (hash_password(PASSWORD), utcnow()))
    return app


@pytest.fixture
def server(hub):
    return hub.state.hub.registry.get(SLUG).build(
        BackendInstance(slug=SLUG, title=SLUG, plugin_id=SLUG))


# ── what it says ──────────────────────────────────────────────────────────

def test_it_names_this_hub_not_a_placeholder():
    """Pasted into an assistant with no connector, the address is the one thing
    it cannot look up."""
    text = integration_prompt(HUB_URL)
    assert HUB_URL in text
    assert "{hub}" not in text


def test_it_can_be_told_what_to_work_on():
    text = integration_prompt(HUB_URL, "the Aenvae dictionary at ~/Projects/aenvae")
    assert text.startswith("The project to do this to: the Aenvae dictionary")


def test_being_told_nothing_adds_nothing():
    assert not integration_prompt(HUB_URL, "   ").startswith("The project")


def test_every_page_it_points_at_exists():
    """A pointer to a page that was renamed sends an assistant looking for
    documentation this hub does not have, and nothing says so."""
    named = set(re.findall(r'read_topic\("([^"]+)"\)', integration_prompt(HUB_URL)))
    assert named, "the prompt is meant to be mostly pointers"
    assert named <= {t.name for t in topics()}


def test_it_names_the_things_the_hub_really_sends():
    """Every one of these is a string an app has to match exactly."""
    from mcphub import appaccess, storage

    text = integration_prompt(HUB_URL)
    for name in (storage.ENV_VAR, appaccess.ENV_VAR, "X-Mcphub-User", "X-Mcphub-Role",
                 "X-Mcphub-Backends", "X-Forwarded-Prefix"):
        assert name in text, name


def test_it_names_the_annotations_levels_are_read_from():
    text = integration_prompt(HUB_URL)
    assert "readOnlyHint" in text and "destructiveHint" in text


def test_it_tells_the_assistant_to_read_rather_than_remember():
    """The pages ship with the build; whatever is pasting this does not."""
    text = integration_prompt(HUB_URL)
    assert "list_topics" in text
    assert "Do not guess" in text


def test_a_missing_template_says_so_rather_than_serving_nothing(monkeypatch):
    from mcphub.plugins.builtin.hub import prompts

    prompts._template.cache_clear()
    monkeypatch.setattr(prompts, "PROMPTS_DIR", Path(tempfile.mkdtemp()))
    try:
        assert "no integration prompt" in integration_prompt(HUB_URL)
    finally:
        prompts._template.cache_clear()


# ── and how it goes out ───────────────────────────────────────────────────

async def test_it_is_a_prompt_on_the_hubs_own_backend(server):
    listed = {p.name for p in await server.list_prompts()}
    assert NAME in listed


async def test_the_prompt_carries_the_text(server):
    got = await server.get_prompt(NAME, {"app": "the dictionary"})
    text = got.messages[0].content.text
    assert "the dictionary" in text
    assert HUB_URL in text


async def test_the_prompt_works_with_no_argument(server):
    """It is optional: an assistant already in the project needs no telling."""
    got = await server.get_prompt(NAME, {})
    assert HUB_URL in got.messages[0].content.text


async def test_the_page_shows_it_with_something_to_copy(hub):
    transport = httpx.ASGITransport(app=hub)
    async with hub.router.lifespan_context(hub):
        client = httpx.AsyncClient(transport=transport, base_url=HUB_URL)
        await client.post("/login", data={"username": "boss", "password": PASSWORD})
        page = await client.get("/integrate")
    assert page.status_code == 200
    assert "readOnlyHint" in page.text
    assert "Copy the prompt" in page.text
    assert NAME in page.text, "the connector route should be offered too"


async def test_the_page_needs_signing_in(hub):
    transport = httpx.ASGITransport(app=hub)
    async with hub.router.lifespan_context(hub):
        client = httpx.AsyncClient(transport=transport, base_url=HUB_URL)
        page = await client.get("/integrate", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")


def test_the_template_ships_with_the_hub():
    """It is read off disk at request time, so a packaging change that drops it
    turns the page into an apology."""
    assert (PROMPTS_DIR / "mcphub_app.md").is_file()
