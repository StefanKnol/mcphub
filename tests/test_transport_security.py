"""Host header handling behind a reverse proxy.

The SDK enforces DNS-rebinding protection on its HTTP transports and, left to
its defaults, accepts only a 127.0.0.1 Host header. Deployed behind a proxy the
Host header is the public name, so every MCP request came back
421 Misdirected Request — *after* a fully successful OAuth exchange, which made
it look like an authentication fault when it was nothing of the kind.

Every earlier test reached the hub on 127.0.0.1, which is exactly the one
Host value that worked, so none of them could have caught it.
"""

from pathlib import Path

import pytest

from mcphub.config import Settings


def settings(public_url: str, port: int = 8080) -> Settings:
    return Settings(data_dir=Path("/tmp"), public_url=public_url, host="0.0.0.0", port=port, dev_mode=False)


def test_public_hostname_is_allowed():
    hosts = settings("https://mcp.example.com").allowed_hosts
    assert "mcp.example.com" in hosts, "the proxy sends the bare hostname on 443"


def test_default_port_form_is_allowed():
    """Some proxies pass the default port through explicitly."""
    assert "mcp.example.com:443" in settings("https://mcp.example.com").allowed_hosts


def test_non_default_port_is_allowed_in_both_forms():
    hosts = settings("https://mcp.example.com:8443").allowed_hosts
    assert "mcp.example.com:8443" in hosts
    assert "mcp.example.com" in hosts


def test_loopback_still_allowed_for_direct_access():
    hosts = settings("https://mcp.example.com", port=6488).allowed_hosts
    assert "127.0.0.1:6488" in hosts
    assert "localhost:6488" in hosts


def test_extra_hosts_from_environment(monkeypatch):
    monkeypatch.setenv("MCPHUB_ALLOWED_HOSTS", "alt.example.com, other.example.com")
    hosts = settings("https://mcp.example.com").allowed_hosts
    assert "alt.example.com" in hosts
    assert "other.example.com" in hosts


def test_unrelated_host_is_not_allowed():
    """The protection has to still protect: this is the rebinding case."""
    hosts = settings("https://mcp.example.com").allowed_hosts
    assert "attacker.example.net" not in hosts


def test_origins_cover_the_public_url():
    origins = settings("https://mcp.example.com").allowed_origins
    assert "https://mcp.example.com" in origins


@pytest.mark.parametrize("url", ["https://mcp.example.com", "http://localhost:8080", "https://a.b.c.example.com"])
def test_configured_host_is_always_present(url):
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname
    assert hostname in settings(url).allowed_hosts
