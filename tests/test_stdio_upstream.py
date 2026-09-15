"""Launching an upstream MCP server rather than connecting to one.

This is what makes npm and PyPI usable as the plugin registry: `npx -y <pkg>`
or `uvx <pkg>` instead of a registry of our own. It also puts third-party code
in its own process, where it cannot reach the credentials the hub holds.
"""

import os

import pytest

from mcphub.plugins.builtin.mcpproxy import _parse_env
from mcphub.plugins.builtin.mcpproxy.upstream import (
    UpstreamConfig,
    UpstreamError,
    _child_env,
    _Stderr,
    _unwrap,
)


# ── transport selection ───────────────────────────────────────────────────

def test_a_command_selects_stdio():
    cfg = UpstreamConfig(command="npx -y @modelcontextprotocol/server-filesystem /data")
    assert cfg.is_stdio
    assert cfg.argv() == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]


def test_a_url_selects_http():
    assert not UpstreamConfig(url="http://host:8043/mcp").is_stdio


def test_whitespace_only_command_is_not_stdio():
    assert not UpstreamConfig(command="   ", url="http://host/mcp").is_stdio


def test_quoted_arguments_survive_parsing():
    cfg = UpstreamConfig(command='uvx some-server --root "/mnt/my files" --flag')
    assert cfg.argv() == ["uvx", "some-server", "--root", "/mnt/my files", "--flag"]


def test_unbalanced_quotes_are_reported_clearly():
    with pytest.raises(UpstreamError) as excinfo:
        UpstreamConfig(command='uvx server --root "unclosed').argv()
    assert "command line" in str(excinfo.value).lower()


def test_label_describes_whichever_transport_is_used():
    assert UpstreamConfig(command="uvx thing").label == "uvx thing"
    assert UpstreamConfig(url="http://h/mcp").label == "http://h/mcp"


# ── child environment ─────────────────────────────────────────────────────

def test_child_env_redirects_caches_onto_the_data_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPHUB_DATA_DIR", str(tmp_path))
    env = _child_env()
    cache = str(tmp_path / "runtime-cache")
    assert env["UV_CACHE_DIR"].startswith(cache)
    assert env["NPM_CONFIG_CACHE"].startswith(cache)
    assert os.path.isdir(cache), "the directory has to exist or the child refuses to start"


def test_child_env_falls_back_when_the_cache_is_unwritable(monkeypatch):
    """Pointing a child at a path it cannot create is worse than its own default.

    uvx refuses to start at all rather than falling back, so an unusable cache
    path must simply not be set.
    """
    monkeypatch.setenv("MCPHUB_DATA_DIR", "/proc/nonexistent-and-unwritable")
    env = _child_env()
    assert "UV_CACHE_DIR" not in env
    assert "NPM_CONFIG_CACHE" not in env
    assert "PATH" in env


def test_child_env_does_not_leak_the_hubs_own_secrets(monkeypatch, tmp_path):
    """A launched server gets a constructed environment, not the hub's."""
    monkeypatch.setenv("MCPHUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOME_HUB_SECRET", "do-not-pass-this-on")
    assert "SOME_HUB_SECRET" not in _child_env()


# ── error reporting ───────────────────────────────────────────────────────

def test_unwrap_digs_out_of_an_exception_group():
    """Transports run in task groups, so failures arrive wrapped."""
    real = ValueError("the actual problem")
    wrapped = BaseExceptionGroup("unhandled errors in a TaskGroup", [real])
    assert _unwrap(wrapped) is real


def test_unwrap_handles_nesting():
    real = ValueError("deep")
    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [real])])
    assert _unwrap(nested) is real


def test_unwrap_leaves_a_plain_exception_alone():
    exc = ValueError("plain")
    assert _unwrap(exc) is exc


def test_stderr_capture_keeps_the_tail():
    err = _Stderr()
    for i in range(50):
        err.write(f"line {i}\n")
    tail = err.tail(count=3)
    assert "line 49" in tail
    assert "line 10" not in tail
    err.close()


def test_stderr_exposes_a_real_descriptor():
    """The transport hands this to a subprocess, which needs a file descriptor."""
    err = _Stderr()
    assert isinstance(err.fileno(), int)
    err.close()


def test_stderr_ignores_blank_lines():
    err = _Stderr()
    err.write("real\n\n\n")
    assert err.tail() == "real"
    err.close()


# ── env parsing for launched servers ──────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("A=1\nB=2", {"A": "1", "B": "2"}),
    ("A=1\n\n# comment\nB=2", {"A": "1", "B": "2"}),
    ("TOKEN=ghp_with=equals", {"TOKEN": "ghp_with=equals"}),
    ("  SPACED = value  ", {"SPACED": "value"}),
    ("", {}),
    ("no-equals-sign", {}),
])
def test_env_parsing(raw, expected):
    assert _parse_env(raw) == expected
