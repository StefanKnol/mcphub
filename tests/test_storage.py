"""Where a backend keeps things between restarts.

Each one gets a directory of its own under the data volume. The hub can hand
that directory to a server it launches, because such a server is its own
subprocess; it can only *name* it to a backend reached over a URL, which may be
in another container entirely. That asymmetry is the whole design, and the
tests below are mostly about not blurring it.
"""

import tempfile
from pathlib import Path

import pytest

from mcphub import storage
from mcphub.plugins.base import BackendInstance
from mcphub.plugins.builtin.mcpproxy import _collect_env


@pytest.fixture
def data_dir() -> Path:
    return Path(tempfile.mkdtemp())


# ── the directory ─────────────────────────────────────────────────────────

def test_each_backend_gets_its_own(data_dir):
    a = storage.ensure(data_dir, "router")
    b = storage.ensure(data_dir, "unraid")
    assert a is not None and b is not None and a != b
    assert a.is_dir() and b.is_dir()


def test_asking_twice_is_not_an_error(data_dir):
    first = storage.ensure(data_dir, "router")
    (first / "app.db").write_text("kept")
    assert storage.ensure(data_dir, "router") == first
    assert (first / "app.db").read_text() == "kept", "it must not be recreated empty"


def test_it_lives_under_the_data_volume(data_dir):
    """Which is what makes it survive a restart and go into the same backup."""
    assert data_dir in storage.path_for(data_dir, "router").parents


@pytest.mark.parametrize("slug", ["../etc", "a/b", "", ".", "..", "/abs"])
def test_a_name_that_is_not_a_directory_is_refused(slug, data_dir):
    """Slugs are already narrower than this, but the path is built from one and
    a rule that only holds somewhere else is not a rule."""
    with pytest.raises(ValueError):
        storage.path_for(data_dir, slug)
    assert storage.ensure(data_dir, slug) is None


def test_a_volume_that_cannot_be_written_reports_it_rather_than_raising(data_dir):
    """A hub whose volume is read-only should still serve every backend that
    does not need storage."""
    readonly = data_dir / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        assert storage.ensure(readonly, "router") is None
    finally:
        readonly.chmod(0o700)


# ── following a rename ────────────────────────────────────────────────────

def test_renaming_a_backend_takes_its_files_with_it(data_dir):
    before = storage.ensure(data_dir, "router")
    (before / "app.db").write_text("kept")

    storage.rename(data_dir, "router", "gateway")
    after = storage.path_for(data_dir, "gateway")
    assert (after / "app.db").read_text() == "kept"
    assert not before.exists(), "leaving the old one behind looks like the rename wiped it"


def test_a_rename_onto_an_existing_directory_leaves_both_alone(data_dir):
    """Better an orphaned directory than one quietly merged into another's."""
    storage.ensure(data_dir, "router")
    occupied = storage.ensure(data_dir, "gateway")
    (occupied / "theirs.db").write_text("someone else's")

    storage.rename(data_dir, "router", "gateway")
    assert (occupied / "theirs.db").read_text() == "someone else's"
    assert storage.path_for(data_dir, "router").is_dir()


def test_renaming_to_the_same_name_does_nothing(data_dir):
    kept = storage.ensure(data_dir, "router")
    (kept / "app.db").write_text("kept")
    storage.rename(data_dir, "router", "router")
    assert (kept / "app.db").read_text() == "kept"


# ── what a launched server is told ────────────────────────────────────────

def instance(env: str = "", *, storage_path: Path | None = None) -> BackendInstance:
    return BackendInstance(slug="router", title="Router", plugin_id="mcp-proxy",
                           config={"env": env}, storage=storage_path)


def test_a_launched_server_is_handed_the_path(data_dir):
    env = _collect_env(instance(storage_path=data_dir / "apps" / "router"))
    assert env[storage.ENV_VAR] == str(data_dir / "apps" / "router")


@pytest.mark.parametrize("written", ["$MCPHUB_STORAGE/app.db", "${MCPHUB_STORAGE}/app.db"])
def test_a_reference_in_a_configured_value_is_expanded(written, data_dir):
    """Almost no server asks for its data path under the hub's name for it; it
    wants DB_PATH or STATE_DIR or whatever it chose. Without this the directory
    is only reachable by servers that happen to agree with us.

    Nothing else would expand it either: a launched server is handed its
    environment directly, with no shell in between.
    """
    path = data_dir / "apps" / "router"
    env = _collect_env(instance(f"DB_PATH={written}", storage_path=path))
    assert env["DB_PATH"] == f"{path}/app.db"


def test_a_name_that_merely_starts_the_same_is_left_alone(data_dir):
    env = _collect_env(instance("X=$MCPHUB_STORAGEROOT", storage_path=data_dir))
    assert env["X"] == "$MCPHUB_STORAGEROOT"


def test_a_backend_with_no_storage_is_told_nothing(data_dir):
    """A read-only volume, say. Better an unset variable than one naming a
    directory that is not there."""
    env = _collect_env(instance("A=1"))
    assert storage.ENV_VAR not in env
    assert env == {"A": "1"}


def test_other_values_are_untouched(data_dir):
    env = _collect_env(instance("A=1\nB=two", storage_path=data_dir))
    assert env["A"] == "1" and env["B"] == "two"


# ── and through the hub ───────────────────────────────────────────────────

@pytest.fixture
def hub():
    from mcphub.app import create_app
    from mcphub.config import Settings

    settings = Settings(data_dir=Path(tempfile.mkdtemp()), public_url="http://localhost:8080",
                        host="127.0.0.1", port=8080, dev_mode=True)
    return create_app(settings).state.hub


def save(hub, slug: str, row=None) -> None:
    from mcphub.web.routes import _save_backend

    _save_backend(hub, slug=slug, plugin_id="mcp-proxy", title="Router", enabled=False,
                  config={"url": "http://10.0.0.1/mcp"}, secrets={}, row=row)


def test_saving_a_backend_creates_its_directory(hub):
    """Before it is started, so the path is there to be bind-mounted into
    whatever is going to need it."""
    save(hub, "router")
    assert storage.path_for(hub.settings.data_dir, "router").is_dir()


def test_a_stored_backend_knows_where_its_directory_is(hub):
    save(hub, "router")
    instance = hub.current_instance("router")
    assert instance is not None
    assert instance.storage == storage.path_for(hub.settings.data_dir, "router")


def test_renaming_through_the_hub_moves_the_files(hub):
    save(hub, "router")
    (storage.path_for(hub.settings.data_dir, "router") / "app.db").write_text("kept")

    save(hub, "gateway", row=hub.backend_row("router"))
    moved = storage.path_for(hub.settings.data_dir, "gateway")
    assert (moved / "app.db").read_text() == "kept"
    assert hub.current_instance("gateway").storage == moved


def test_deleting_a_backend_leaves_its_files_alone(hub):
    """Unmounting is reversible, and a dropped database is not. The confirmation
    says so rather than the hub deciding."""
    from mcphub.web.routes import _revoke_backend_credentials

    save(hub, "router")
    kept = storage.path_for(hub.settings.data_dir, "router")
    (kept / "app.db").write_text("kept")

    _revoke_backend_credentials(hub, "router")
    hub.db.execute("DELETE FROM backends WHERE slug = 'router'")
    assert (kept / "app.db").read_text() == "kept"


# ── shared between versions and between accounts ──────────────────────────

def variant_storage(hub, slug: str, version: str):
    """The directory a mounted variant is actually given."""
    instance = hub.current_instance(slug)
    per_version = bool(instance.config.get(storage.PER_VERSION))
    return storage.ensure(hub.settings.data_dir, slug, version if per_version else "")


def test_two_versions_of_one_backend_share_a_directory(hub):
    """Which is the point. Someone trying a new release of the server should
    still be working on the same dictionary as the colleague on the old one —
    the data is the thing they share, and the version is not."""
    save(hub, "aenvae")
    stable = variant_storage(hub, "aenvae", "")
    testing = variant_storage(hub, "aenvae", "0.4.0")
    assert stable == testing

    (stable / "words.db").write_text("one dictionary")
    assert (testing / "words.db").read_text() == "one dictionary"


def test_every_account_shares_it_too(hub):
    """Backends are shared, so their storage is. A per-account directory would
    give two people editing one dictionary two dictionaries."""
    save(hub, "aenvae")
    assert hub.current_instance("aenvae").storage == storage.path_for(
        hub.settings.data_dir, "aenvae"), "nothing about the path depends on who is asking"


def test_a_pinned_version_does_not_lose_its_storage(hub):
    """`variant()` builds a new instance for a pinned version. Listing its
    fields by hand dropped the storage path, so a pinned account got a server
    with nowhere to write — and only a pinned one, which is the hardest kind of
    difference to notice."""
    from dataclasses import fields

    from mcphub.plugins.builtin.mcpproxy import PLUGIN
    from mcphub.plugins.base import BackendInstance

    original = BackendInstance(
        slug="aenvae", title="Aenvae", plugin_id="mcp-proxy",
        config={"command": "uvx aenvae",
                "registry_package": {"registryType": "pypi", "identifier": "aenvae",
                                     "runtime": "uvx", "args": []}},
        storage=Path("/data/apps/aenvae"))
    pinned = PLUGIN.variant(original, "0.4.0")

    assert pinned.config["command"] == "uvx aenvae==0.4.0", "it must still pin the version"
    carried = {f.name for f in fields(BackendInstance)} - {"config"}
    for name in carried:
        assert getattr(pinned, name) == getattr(original, name), (
            f"a variant dropped {name!r}; build it with dataclasses.replace so a "
            f"field added later is carried too"
        )


def test_versions_can_be_separated_when_they_cannot_share(hub):
    """A derived index whose format changed, say — not the common case, but a
    real one, and sharing a directory would corrupt it."""
    save(hub, "indexer")
    hub.db.execute("UPDATE backends SET config_json = ? WHERE slug = 'indexer'",
                   ('{"' + storage.PER_VERSION + '": true}',))
    default = variant_storage(hub, "indexer", "")
    pinned = variant_storage(hub, "indexer", "0.4.0")
    assert default != pinned
    assert pinned.name == "indexer@0.4.0"


@pytest.mark.parametrize("version", ["../etc", "a/b", "$(x)", "a" * 70])
def test_a_version_that_is_not_a_directory_name_is_refused(version, data_dir):
    with pytest.raises(ValueError):
        storage.path_for(data_dir, "router", version)
    assert storage.ensure(data_dir, "router", version) is None


# ── and what a running backend is actually handed ─────────────────────────

class Recording:
    """A plugin that reports the storage each variant was built with."""

    id, name, description, fields = "recording", "Recording", "d", ()
    review_before_enable = False

    def __init__(self) -> None:
        self.built: list[tuple[str, Path | None]] = []

    def build(self, instance):
        from mcp.server.mcpserver import MCPServer

        self.built.append((instance.config.get("upstream_version", ""), instance.storage))
        return MCPServer(instance.title)

    def variant(self, instance, version):
        from dataclasses import replace

        return replace(instance, config={**instance.config, "upstream_version": version})

    def fields_for(self, instance):
        return ()

    async def check(self, instance):  # pragma: no cover
        from mcphub.plugins.base import CheckResult

        return CheckResult(True, "ok")


async def started(hub, plugin, *, per_version: bool, versions: tuple[str, ...]):
    from mcphub.auth.provider import HubOAuthProvider
    from mcphub.mounts import MountManager
    from mcphub.plugins.base import BackendInstance

    # Built directly: `hub.mounts` only exists once the app's lifespan has run,
    # and what is under test is one method of it.
    mounts = MountManager(app=None, provider=HubOAuthProvider(hub.db),
                          settings=hub.settings, db=hub.db)
    instance = BackendInstance(
        slug="aenvae", title="Aenvae", plugin_id="recording",
        config={storage.PER_VERSION: True} if per_version else {})
    for version in versions:
        variant = await mounts._start_variant(plugin, instance, version)
        variant.stop.set()
    return plugin.built


async def test_a_running_backend_is_handed_a_directory_that_exists(hub):
    built = await started(hub, Recording(), per_version=False, versions=("",))
    _, path = built[0]
    assert path is not None and path.is_dir()


async def test_both_running_versions_are_handed_the_same_directory(hub):
    """The end of the chain: two servers running at once, one directory.

    This is what the dictionary case needs, and the only place it is decided —
    so it is checked against real started variants rather than the helper that
    computes the path.
    """
    built = await started(hub, Recording(), per_version=False, versions=("", "0.4.0"))
    assert {version for version, _ in built} == {"", "0.4.0"}
    assert len({path for _, path in built}) == 1


async def test_separated_versions_are_handed_different_ones(hub):
    built = await started(hub, Recording(), per_version=True, versions=("", "0.4.0"))
    assert len({path for _, path in built}) == 2
    assert all(path is not None and path.is_dir() for _, path in built)
