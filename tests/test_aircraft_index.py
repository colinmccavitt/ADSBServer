"""Tests for app.aircraft_index — the SQLite-backed ICAO lookup that replaces a
~1.1 GB in-memory dict of ~616k aircraft.

Covers the round trip, freshness detection (so a re-downloaded snapshot rebuilds
and an unchanged one does not), and the streaming snapshot reader.
"""

import gzip
import json

import pytest

from app.aircraft_db import AircraftDB
from app.aircraft_index import AircraftIndex, iter_snapshot


def normalize(entry):
    """Same shape AircraftDB._normalize_local produces."""
    return {
        "registration": entry.get("reg") or None,
        "aircraft_type": entry.get("icaotype") or entry.get("short_type") or None,
        "aircraft_model": entry.get("model") or None,
        "manufacturer": entry.get("manufacturer") or None,
        "owner_operator": entry.get("ownop") or None,
        "year_built": str(entry["year"]) if entry.get("year") else None,
        "is_military": entry.get("mil") if isinstance(entry.get("mil"), bool) else None,
    }


ENTRIES = [
    {"icao": "ac738e", "reg": "N901GW", "ownop": "GILBERT WRIGHT", "mil": False},
    {"icao": "a2c27e", "reg": "N556WN", "icaotype": "B737", "model": "737-700",
     "manufacturer": "BOEING", "year": 2004, "mil": False},
    {"icao": "ae1234", "reg": "165939", "icaotype": "C130", "mil": True},
]


@pytest.fixture
def snapshot(tmp_path):
    """A gzipped NDJSON snapshot, the format the real download uses."""
    p = tmp_path / "basic-ac-db.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for e in ENTRIES:
            fh.write(json.dumps(e) + "\n")
    return p


@pytest.fixture
def index(tmp_path):
    idx = AircraftIndex(tmp_path / "idx.sqlite")
    yield idx
    idx.close()


def test_lookup_round_trips_normalized_fields(index, snapshot):
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    got = index.lookup("A2C27E")
    assert got == {
        "registration": "N556WN",
        "aircraft_type": "B737",
        "aircraft_model": "737-700",
        "manufacturer": "BOEING",
        "owner_operator": None,
        "year_built": "2004",
        "is_military": False,
    }


def test_icao_lookup_is_case_normalized_by_the_caller(index, snapshot):
    """The index stores upper-case keys; AircraftDB upper-cases before calling."""
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.lookup("A2C27E") is not None
    assert index.lookup("a2c27e") is None  # caller's job, asserted so it stays so


def test_unknown_icao_returns_none_and_has_is_false(index, snapshot):
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.lookup("FFFFFF") is None
    assert index.has("FFFFFF") is False
    assert index.has("AC738E") is True


def test_military_flag_survives_the_integer_round_trip(index, snapshot):
    """SQLite has no bool; None/True/False must all come back intact."""
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.lookup("AE1234")["is_military"] is True
    assert index.lookup("AC738E")["is_military"] is False

    index.rebuild([("NOMIL0", {"is_military": None})], snapshot)
    assert index.lookup("NOMIL0")["is_military"] is None


def test_count_reflects_the_snapshot(index, snapshot):
    n = index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert n == len(ENTRIES)
    assert index.count == len(ENTRIES)


def test_index_is_reused_when_the_snapshot_is_unchanged(index, snapshot):
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.is_current_for(snapshot) is True


def test_index_is_stale_after_the_snapshot_changes(index, snapshot, tmp_path):
    """A re-downloaded snapshot must trigger a rebuild, or lookups go stale."""
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.is_current_for(snapshot)

    import os
    with gzip.open(snapshot, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"icao": "beef01", "reg": "G-NEW"}) + "\n")
    # Force a distinct mtime; fingerprint is (size, mtime).
    st = os.stat(snapshot)
    os.utime(snapshot, (st.st_atime + 10, st.st_mtime + 10))

    assert index.is_current_for(snapshot) is False
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    assert index.lookup("BEEF01")["registration"] == "G-NEW"
    assert index.lookup("A2C27E") is None, "rebuild left rows from the old snapshot"


def test_empty_index_is_never_considered_current(tmp_path, snapshot):
    """Guards the fallback: an empty index must not be mistaken for a good one."""
    idx = AircraftIndex(tmp_path / "empty.sqlite")
    try:
        assert idx.count == 0
        assert idx.is_current_for(snapshot) is False
    finally:
        idx.close()


def test_iter_snapshot_reads_a_keyed_object_form(tmp_path):
    """The loader tolerated a single JSON object keyed by ICAO; so must this."""
    p = tmp_path / "keyed.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump({"abc123": {"reg": "N1"}, "def456": {"reg": "N2"}}, fh)
    pairs = dict(iter_snapshot(p, normalize))
    assert set(pairs) == {"ABC123", "DEF456"}
    assert pairs["ABC123"]["registration"] == "N1"


def test_aircraft_db_falls_back_to_the_dict_when_the_index_fails(monkeypatch):
    """Worst case must be today's behaviour, not a broken lookup path."""
    db = AircraftDB()
    monkeypatch.setattr(
        "app.aircraft_db.AircraftIndex",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert db._try_build_index() is False
    assert db._index is None
    # Dict path still answers.
    db._db = {"ABC123": {"reg": "N123AB"}}
    assert db.has_icao("abc123") is True
    assert db.entry_count == 1


async def test_aircraft_db_lookup_prefers_the_index(index, snapshot, monkeypatch):
    db = AircraftDB()
    index.rebuild(iter_snapshot(snapshot, normalize), snapshot)
    db._index = index
    # If the index answers, no HTTP fallback should be attempted.
    async def boom(_icao):
        raise AssertionError("hexdb fallback used for a locally indexed ICAO")
    monkeypatch.setattr(db, "_fetch_hexdb", boom)

    got = await db.lookup("a2c27e")
    assert got["registration"] == "N556WN"
    assert db.entry_count == len(ENTRIES)
