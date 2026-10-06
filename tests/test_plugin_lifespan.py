"""A plugin's own lifespan, held open by the hub while the backend is mounted.

`Plugin.lifespan` is where a backend starts what it keeps running: a timer, a
watcher, a pool. The hub enters it after the backend's server is up and leaves
it on unmount, in its own event loop, for the default variant only, with the
same instance `build` receives. Its failures are its own: the tools stay up.
"""

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from mcphub.plugins.base import OPTIONAL_ATTRIBUTES, CheckResult, PluginDefaults


def make_ticker_plugin(seen: dict):
    """A plugin whose lifespan records when it runs and what it was given."""

    class Ticker(PluginDefaults):
        id, name, description, fields = "ticker", "Ticker", "ticks", ()

        def uses_storage(self, instance):
            return True

        def build(self, instance):
            seen["build_storage"] = instance.storage
            return MCPServer(instance.title)

        @contextlib.asynccontextmanager
        async def lifespan(self, instance):
            seen["entered"] = seen.get("entered", 0) + 1
            seen["lifespan_storage"] = instance.storage
            seen["loop"] = asyncio.get_running_loop()
            task = asyncio.create_task(self._tick(seen))
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                seen["exited"] = seen.get("exited", 0) + 1

        @staticmethod
        async def _tick(seen):
            while True:
                seen["ticks"] = seen.get("ticks", 0) + 1
                await asyncio.sleep(0.01)

        async def check(self, instance):  # pragma: no cover
            return CheckResult(True, "ok")

    return Ticker()


class StartFails(PluginDefaults):
    id, name, description, fields = "startfails", "Start fails", "s", ()

    def build(self, instance):
        return MCPServer(instance.title)

    @contextlib.asynccontextmanager
    async def lifespan(self, instance):
        raise RuntimeError("no background work today")
        yield  # pragma: no cover

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")


class StopFails(PluginDefaults):
    id, name, description, fields = "stopfails", "Stop fails", "s", ()

    def build(self, instance):
        return MCPServer(instance.title)

    @contextlib.asynccontextmanager
    async def lifespan(self, instance):
        yield
        raise RuntimeError("could not stop cleanly")

    async def check(self, instance):  # pragma: no cover
        return CheckResult(True, "ok")


@pytest.fixture
def seen():
    return {}


@pytest.fixture
def hub(seen):
    from mcphub.app import create_app
    from mcphub.config import Settings
    from mcphub.db import utcnow

    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://127.0.0.1:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    app = create_app(settings)
    state = app.state.hub
    for plugin in (make_ticker_plugin(seen), StartFails(), StopFails()):
        state.registry.register(plugin)
    for slug in ("ticker", "startfails", "stopfails"):
        state.db.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, created_at, "
            "updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (slug, slug, slug.title(), json.dumps({}), utcnow(), utcnow()))
    return app


def test_the_lifespan_runs_while_the_backend_is_mounted_and_not_after(hub, seen):
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080"):
        assert seen["entered"] == 1
        assert seen.get("exited", 0) == 0
        assert seen["lifespan_storage"] is not None
        assert seen["lifespan_storage"] == seen["build_storage"], "the same instance build received"
    assert seen["exited"] == 1
    assert seen["ticks"] >= 1, "the task it started ran in the hub's loop"


def test_a_pinned_version_does_not_enter_a_second_lifespan(hub, seen):
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080") as client:
        mounts = hub.state.hub.mounts
        client.portal.call(mounts.variant, "ticker", "v2")
        assert len(mounts._mounted["ticker"].variants) == 2
        assert seen["entered"] == 1, "a version is the same backend, not a second one"
    assert seen["exited"] == 1


def test_a_lifespan_that_fails_to_start_leaves_the_tools_up(hub):
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080"):
        mounts = hub.state.hub.mounts
        assert "startfails" in {mounted.slug for mounted in mounts.active()}
        assert mounts._mounted["startfails"].server is not None


def test_a_lifespan_that_fails_to_stop_does_not_block_the_unmount(hub):
    from starlette.testclient import TestClient

    with TestClient(hub, base_url="http://127.0.0.1:8080"):
        mounts = hub.state.hub.mounts
        assert "stopfails" in {mounted.slug for mounted in mounts.active()}
    assert mounts.active() == []


async def test_the_default_is_a_context_that_does_nothing():
    async with PluginDefaults().lifespan(None):
        pass


def test_the_hook_is_an_optional_attribute_the_hub_validates():
    assert "lifespan" in OPTIONAL_ATTRIBUTES
