"""Tests for scripts/build_validation_artifacts.py.

The artifacts are a projection of the journal, so the properties that matter
are: every record traces to a journal entry, the outcome classification is
right at each boundary, and a rebuild over an unchanged journal is
byte-identical (otherwise the Merkle root in `make verify` would churn for
no reason and stop meaning anything).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_validation_artifacts as bva  # noqa: E402


def _entry(seq, etype, payload, **kw):
    e = {
        "seq": seq,
        "type": etype,
        "timestamp": f"2026-08-{10 + seq:02d}T00:00:00+00:00",
        "payload": payload,
        "prev_hash": "0" * 64,
        "entry_hash": f"{seq:064d}",
    }
    e.update(kw)
    return e


def _write(tmp_path, entries, name="journal.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return p


# --- reading -------------------------------------------------------------

def test_reads_entries_and_tags_source(tmp_path):
    p = _write(tmp_path, [_entry(1, "snapshot", {"underlying": "SPY"})])
    got = bva.read_entries([p])
    assert len(got) == 1
    assert got[0]["_source"] == "journal.jsonl"


def test_skips_blank_and_malformed_lines(tmp_path):
    p = tmp_path / "j.jsonl"
    good = json.dumps(_entry(1, "snapshot", {"underlying": "SPY"}))
    # a truncated final line is what a session killed mid-write leaves behind
    p.write_text(good + "\n\n{not json\n" + '{"no_type": true}\n')
    assert len(bva.read_entries([p])) == 1


def test_missing_file_is_not_an_error(tmp_path):
    assert bva.read_entries([tmp_path / "nope.jsonl"]) == []


# --- outcome classification ----------------------------------------------

def test_no_choice_is_abstained(tmp_path):
    e = [_entry(1, "committee_decision", {"choice_id": None, "underlying": "SPY"})]
    doc = bva.build_trade_intents(e)
    assert doc["records"][0]["outcome"] == "ABSTAINED"


def test_failed_blind_review_is_vetoed(tmp_path):
    e = [_entry(1, "committee_decision",
                {"choice_id": 3, "thesis_ok": True, "blind_ok": False})]
    assert bva.build_trade_intents(e)["records"][0]["outcome"] == "VETOED"


def test_failed_thesis_check_is_vetoed(tmp_path):
    e = [_entry(1, "committee_decision",
                {"choice_id": 3, "thesis_ok": False, "blind_ok": True})]
    assert bva.build_trade_intents(e)["records"][0]["outcome"] == "VETOED"


def test_both_gates_pass_is_selected(tmp_path):
    e = [_entry(1, "committee_decision",
                {"choice_id": 3, "thesis_ok": True, "blind_ok": True})]
    assert bva.build_trade_intents(e)["records"][0]["outcome"] == "SELECTED"


def test_realized_pnl_joins_on_snapshot_hash():
    entries = [
        _entry(1, "committee_decision",
               {"choice_id": 1, "thesis_ok": True, "blind_ok": True,
                "snapshot_hash": "abc"}),
        _entry(2, "close", {"snapshot_hash": "abc", "realized_pnl": 76.0}),
    ]
    rec = bva.build_trade_intents(entries)["records"][0]
    assert rec["realizedPnl"] == 76.0


def test_unmatched_close_leaves_pnl_absent():
    entries = [
        _entry(1, "committee_decision",
               {"choice_id": 1, "thesis_ok": True, "blind_ok": True,
                "snapshot_hash": "abc"}),
        _entry(2, "close", {"snapshot_hash": "other", "realized_pnl": 76.0}),
    ]
    assert "realizedPnl" not in bva.build_trade_intents(entries)["records"][0]


# --- risk checks ---------------------------------------------------------

def test_riskguard_and_veto_land_in_one_stream():
    entries = [
        _entry(1, "verdict", {"decision": "DENY", "reason": "max loss",
                              "underlying": "SPY"}),
        _entry(2, "veto", {"thesis_ok": True, "blind_ok": False,
                           "blind_reason": "economics"}),
    ]
    doc = bva.build_risk_checks(entries)
    gates = [r["gate"] for r in doc["records"]]
    assert gates == ["RiskGuard", "DualModelVeto"]
    assert doc["records"][0]["decision"] == "DENY"
    assert doc["records"][1]["decision"] == "VETO"


def test_veto_passing_both_gates_is_pass():
    e = [_entry(1, "veto", {"thesis_ok": True, "blind_ok": True})]
    assert bva.build_risk_checks(e)["records"][0]["decision"] == "PASS"


# --- checkpoints ---------------------------------------------------------

def test_snapshot_funnel_fields():
    e = [_entry(1, "snapshot", {"underlying": "SPY", "spot": 769.35,
                                "realized_vol": 0.0952,
                                "total_candidates": 632, "candidate_count": 12})]
    rec = bva.build_strategy_checkpoints(e)["records"][0]
    assert rec["totalCandidates"] == 632
    assert rec["shownToCommittee"] == 12


def test_non_snapshot_entries_are_ignored():
    e = [_entry(1, "veto", {"thesis_ok": True, "blind_ok": True})]
    assert bva.build_strategy_checkpoints(e)["records"] == []


# --- whole-file behaviour ------------------------------------------------

def test_ids_are_sequential_and_prefixed():
    e = [_entry(i, "snapshot", {"underlying": "SPY"}) for i in range(1, 4)]
    ids = [r["id"] for r in bva.build_strategy_checkpoints(e)["records"]]
    assert ids == ["SC-0001", "SC-0002", "SC-0003"]


def test_empty_journal_yields_zero_records(tmp_path):
    p = _write(tmp_path, [])
    counts = bva.build_all([p], tmp_path / "out")
    assert set(counts.values()) == {0}


def test_rebuild_is_byte_identical(tmp_path):
    entries = [
        _entry(1, "snapshot", {"underlying": "SPY", "spot": 769.35,
                               "total_candidates": 632, "candidate_count": 12}),
        _entry(2, "committee_decision", {"choice_id": 1, "thesis_ok": True,
                                         "blind_ok": True, "snapshot_hash": "h"}),
        _entry(3, "verdict", {"decision": "ALLOW", "underlying": "SPY"}),
    ]
    p = _write(tmp_path, entries)
    out = tmp_path / "out"
    bva.build_all([p], out)
    first = {f.name: f.read_text() for f in out.glob("*.json")}
    bva.build_all([p], out)
    second = {f.name: f.read_text() for f in out.glob("*.json")}
    assert first == second


def test_every_record_carries_its_journal_entry_hash(tmp_path):
    entries = [
        _entry(1, "snapshot", {"underlying": "SPY"}),
        _entry(2, "committee_decision", {"choice_id": 1, "thesis_ok": True,
                                         "blind_ok": True}),
        _entry(3, "verdict", {"decision": "ALLOW"}),
    ]
    p = _write(tmp_path, entries)
    out = tmp_path / "out"
    bva.build_all([p], out)
    for f in out.glob("*.json"):
        for rec in json.loads(f.read_text())["records"]:
            assert rec["entryHash"], f"{f.name} record has no journal entry hash"
