"""The verified-server list.

A badge is only worth showing if it reports a measurement. These tests pin the
things that would quietly turn it back into a claim: an entry that was never
checked must not read as verified, and one checked long ago must say so.
"""

import json
from datetime import date, timedelta

import pytest

from mcphub import verified
from mcphub.verified import STALE_AFTER_DAYS, Verification


def entry(**kwargs) -> Verification:
    return Verification(name="x/y", **kwargs)


def test_unchecked_is_not_verified():
    v = entry(status="unchecked")
    assert not v.ok
    assert v.summary == "Not verified"


def test_failed_is_not_verified():
    assert not entry(status="failed", last_verified=date.today().isoformat()).ok


def test_unavailable_is_not_verified():
    """A server that is not in the registry cannot have been verified."""
    assert not entry(status="unavailable", last_verified=date.today().isoformat()).ok


def test_a_recent_check_is_verified_and_reports_the_count():
    v = entry(status="ok", tool_count=24, last_verified=date.today().isoformat())
    assert v.ok and not v.stale
    assert "24 tools" in v.summary


def test_an_old_check_is_marked_stale():
    old = (date.today() - timedelta(days=STALE_AFTER_DAYS + 1)).isoformat()
    assert entry(status="ok", tool_count=3, last_verified=old).stale


def test_a_check_with_no_date_is_stale():
    """An entry someone hand-edited to ok without a date claims nothing."""
    assert entry(status="ok", tool_count=3).stale


def test_a_malformed_date_is_stale_rather_than_crashing():
    assert entry(status="ok", tool_count=3, last_verified="last tuesday").stale


def test_missing_file_yields_no_verifications(monkeypatch, tmp_path):
    monkeypatch.setattr(verified, "DATA_FILE", tmp_path / "absent.json")
    verified._load.cache_clear()
    assert verified.lookup("anything") is None
    assert verified.all_verified() == []
    verified._load.cache_clear()


def test_malformed_file_does_not_break_the_page(monkeypatch, tmp_path):
    bad = tmp_path / "verified.json"
    bad.write_text("{not json")
    monkeypatch.setattr(verified, "DATA_FILE", bad)
    verified._load.cache_clear()
    assert verified.all_verified() == []
    verified._load.cache_clear()


def test_shipped_file_is_valid_and_machine_written():
    payload = json.loads(verified.DATA_FILE.read_text())
    assert payload["schemaVersion"] == 1
    for row in payload["servers"]:
        assert row.get("name")
        assert row.get("status") in {"ok", "failed", "unavailable", "unchecked"}
        if row["status"] == "ok":
            assert row.get("lastVerified"), f"{row['name']} claims ok with no check date"
            assert row.get("toolCount"), f"{row['name']} claims ok with no tools observed"


@pytest.mark.parametrize("name", ["io.github.StefanKnol/mikrotik-mcp"])
def test_shipped_entries_load(name):
    assert verified.lookup(name) is not None


def test_shipped_entries_are_keyed_by_registry_name():
    """A badge is matched against the registry entry's name.

    An entry keyed by anything else - a bare package name, say - can never
    attach to a search result, so it would silently never appear.
    """
    for row in json.loads(verified.DATA_FILE.read_text())["servers"]:
        assert "/" in row["name"], (
            f"{row['name']!r} is not a registry name, so no search result will ever match it"
        )
