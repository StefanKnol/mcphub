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
