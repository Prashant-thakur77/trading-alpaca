"""Tests for llm/cache.py — the prompt cache / audit trail / replay corpus.

One JSON file per call, keyed by prompt_hash. This is simultaneously the cost
saver (skip a paid call on a hit), the audit record (what was asked, what came
back), and the deterministic-replay corpus (golden-file tests read these back
verbatim) — so a cache hit must reproduce the record byte-for-byte, and a
failed parse must be persisted too, not silently dropped.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.cache import PromptCache


def test_miss_returns_none(tmp_path):
    cache = PromptCache(tmp_path)
    assert cache.get("deadbeef") is None


def test_put_then_get_returns_identical_record(tmp_path):
    cache = PromptCache(tmp_path)
    record = {
        "prompt": "is IV rich?",
        "model": "claude-haiku-4-5",
        "raw_response": '{"probability": 0.6, "reasoning": "..."}',
        "parsed": {"probability": 0.6, "reasoning": "..."},
        "timestamp": "2026-08-29T00:00:00+00:00",
        "error": "",
    }
    path = cache.put("abc123", record)
    assert path.exists()
    assert cache.get("abc123") == record


def test_failed_parse_is_persisted_with_error(tmp_path):
    cache = PromptCache(tmp_path)
    record = {
        "prompt": "is IV rich?",
        "model": "claude-haiku-4-5",
        "raw_response": "I refuse to answer in JSON.",
        "parsed": None,
        "timestamp": "2026-08-29T00:00:01+00:00",
        "error": "could not extract JSON from claude response text",
    }
    cache.put("badhash", record)
    got = cache.get("badhash")
    assert got["parsed"] is None
    assert got["error"] == "could not extract JSON from claude response text"
    assert got["raw_response"] == "I refuse to answer in JSON."


def test_one_file_per_hash(tmp_path):
    cache = PromptCache(tmp_path)
    cache.put("hash1", {"prompt": "a", "model": "m", "raw_response": "", "parsed": None,
                         "timestamp": "t", "error": ""})
    cache.put("hash2", {"prompt": "b", "model": "m", "raw_response": "", "parsed": None,
                         "timestamp": "t", "error": ""})
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 2


def test_truncated_record_reads_as_a_miss_not_an_exception(tmp_path):
    # An interrupted write (or any corrupt file) must not permanently break
    # the /judge replay path with a JSONDecodeError. A record that cannot be
    # read is a cache miss: the caller re-runs the call.
    cache = PromptCache(tmp_path)
    (tmp_path / "half.json").write_text('{"prompt": "is IV ri')
    assert cache.get("half") is None


def test_record_that_is_not_an_object_reads_as_a_miss(tmp_path):
    cache = PromptCache(tmp_path)
    (tmp_path / "weird.json").write_text('["not", "a", "record"]')
    assert cache.get("weird") is None


def test_put_leaves_no_temporary_file_behind(tmp_path):
    cache = PromptCache(tmp_path)
    cache.put("h", {"prompt": "a", "model": "m", "raw_response": "", "parsed": None,
                     "timestamp": "t", "error": ""})
    assert [p.name for p in tmp_path.iterdir()] == ["h.json"]


def test_a_failed_put_leaves_the_previous_record_intact(tmp_path):
    # write_text truncates in place: a serialization failure (or a crash) part
    # way through used to leave a half-written record on disk. Temp-then-
    # rename means the old record survives a failed write untouched.
    cache = PromptCache(tmp_path)
    good = {"prompt": "a", "model": "m", "raw_response": "r", "parsed": None,
            "timestamp": "t", "error": ""}
    cache.put("h", good)

    unserializable = {(1, 2): "tuple keys are not JSON"}
    try:
        cache.put("h", unserializable)
    except Exception:
        pass

    assert cache.get("h") == good
    assert [p.name for p in tmp_path.iterdir()] == ["h.json"]


def test_cache_dir_created_if_missing(tmp_path):
    nested = tmp_path / "nested" / "cache"
    cache = PromptCache(nested)
    cache.put("h", {"prompt": "a", "model": "m", "raw_response": "", "parsed": None,
                     "timestamp": "t", "error": ""})
    assert nested.exists()
    assert cache.get("h") is not None
