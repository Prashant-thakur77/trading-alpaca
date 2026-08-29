"""Tests for journal.py — hash-chained append-only decision journal.

Hard rule 5: every decision appends one JSONL entry with
prev_hash = SHA-256 of the previous entry. Past entries are never edited.
"""
import json
import multiprocessing
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from journal import Journal, GENESIS_HASH, verify_chain


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _hammer(path: str, count: int, label: int) -> None:
    """Append `count` entries from a separate process (module-level so it pickles)."""
    from journal import Journal as J
    j = J(path)
    for i in range(count):
        j.append("proposal", {"who": label, "i": i})


class TestAppend:
    def test_first_entry_uses_genesis_prev_hash(self, journal):
        """The first entry has no predecessor, so prev_hash is the genesis constant."""
        entry = journal.append("proposal", {"symbol": "SPY"})
        assert entry["prev_hash"] == GENESIS_HASH

    def test_second_entry_prev_hash_is_first_entry_hash(self, journal):
        """Each entry chains to the one before it."""
        first = journal.append("proposal", {"symbol": "SPY"})
        second = journal.append("verdict", {"decision": "ALLOW"})
        assert second["prev_hash"] == first["entry_hash"]

    def test_entry_records_type_and_payload(self, journal):
        entry = journal.append("veto", {"reason": "models disagree"})
        assert entry["type"] == "veto"
        assert entry["payload"] == {"reason": "models disagree"}

    def test_entry_has_utc_timestamp(self, journal):
        entry = journal.append("fill", {"qty": 1})
        assert entry["timestamp"].endswith("+00:00")

    def test_appends_one_jsonl_line_per_entry(self, journal):
        journal.append("proposal", {"n": 1})
        journal.append("proposal", {"n": 2})
        journal.append("proposal", {"n": 3})
        lines = journal.path.read_text().strip().splitlines()
        assert len(lines) == 3
        assert [json.loads(ln)["payload"]["n"] for ln in lines] == [1, 2, 3]

    def test_entry_hash_covers_payload(self, journal):
        """Two entries with different payloads must not share a hash."""
        a = Journal(journal.path.parent / "a.jsonl").append("t", {"v": 1})
        b = Journal(journal.path.parent / "b.jsonl").append("t", {"v": 2})
        assert a["entry_hash"] != b["entry_hash"]

    def test_survives_reopen(self, tmp_path):
        """A new Journal over an existing file continues the chain, not restart it."""
        path = tmp_path / "j.jsonl"
        first = Journal(path).append("proposal", {"n": 1})
        second = Journal(path).append("proposal", {"n": 2})
        assert second["prev_hash"] == first["entry_hash"]


class TestVerifyChain:
    def test_empty_journal_is_valid(self, journal):
        ok, err = verify_chain(journal.path)
        assert ok is True
        assert err == ""

    def test_intact_chain_is_valid(self, journal):
        for i in range(5):
            journal.append("proposal", {"n": i})
        ok, err = verify_chain(journal.path)
        assert ok is True, err

    def test_detects_edited_payload(self, journal):
        """Editing a past entry breaks its own hash — rule 5's whole point."""
        journal.append("proposal", {"n": 1})
        journal.append("proposal", {"n": 2})
        lines = journal.path.read_text().strip().splitlines()
        tampered = json.loads(lines[0])
        tampered["payload"]["n"] = 999
        lines[0] = json.dumps(tampered)
        journal.path.write_text("\n".join(lines) + "\n")

        ok, err = verify_chain(journal.path)
        assert ok is False
        assert "entry 0" in err

    def test_detects_deleted_entry(self, journal):
        """Removing a middle entry breaks the prev_hash link of its successor."""
        journal.append("proposal", {"n": 1})
        journal.append("proposal", {"n": 2})
        journal.append("proposal", {"n": 3})
        lines = journal.path.read_text().strip().splitlines()
        del lines[1]
        journal.path.write_text("\n".join(lines) + "\n")

        ok, err = verify_chain(journal.path)
        assert ok is False
        assert "prev_hash" in err

    def test_detects_missing_file(self, tmp_path):
        ok, err = verify_chain(tmp_path / "nope.jsonl")
        assert ok is False
        assert "not found" in err.lower()


class TestConcurrentAppends:
    """The scan process and the exit-monitor process journal at the same time.

    If the tail is read before the exclusive lock is taken, two writers see the
    same predecessor and emit two entries with the same seq and prev_hash —
    the chain forks and `make verify-journal`, a judged artifact, then fails
    permanently. Nothing can repair it, because entries are never rewritten.
    """

    def test_concurrent_writers_do_not_fork_the_chain(self, tmp_path):
        path = str(tmp_path / "j.jsonl")
        Journal(path)   # create up front so every child continues one chain

        writers, per_writer = 4, 25
        procs = [multiprocessing.Process(target=_hammer, args=(path, per_writer, k))
                 for k in range(writers)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]

        entries = Journal(path).entries()
        assert len(entries) == writers * per_writer
        # Every entry must occupy a distinct, gapless position in the chain.
        assert [e["seq"] for e in entries] == list(range(writers * per_writer))
        ok, err = verify_chain(path)
        assert ok, err
