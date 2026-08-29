"""Tests for llm/client.py — the claude CLI subprocess wrapper.

This function must NEVER raise: an LLM failure aborts a trade decision into
ABSTAIN, not a crashed scan (spec 4.4 "fail loud on data, fail soft on the
LLM"). No test here touches the network or spawns a real subprocess — every
call goes through an injected fake `runner`.
"""
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.client import call_claude, LLMResponse


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                        stdout=stdout, stderr=stderr)


def _envelope(result_text, cost=0.019):
    return json.dumps({"result": result_text, "total_cost_usd": cost})


def test_happy_path_plain_json():
    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope('{"probability": 0.6, "reasoning": "ok"}'))

    resp = call_claude("some prompt", runner=runner)
    assert isinstance(resp, LLMResponse)
    assert resp.ok is True
    assert resp.parsed == {"probability": 0.6, "reasoning": "ok"}
    assert resp.error == ""
    assert resp.cost_usd == 0.019
    assert resp.model == "claude-haiku-4-5"


def test_prompt_hash_is_sha256_of_model_and_prompt():
    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope('{"a": 1}'))

    resp = call_claude("hello", model="claude-sonnet-5", runner=runner)
    expected = hashlib.sha256("claude-sonnet-5\nhello".encode()).hexdigest()
    assert resp.prompt_hash == expected


def test_markdown_fenced_json_is_extracted():
    text = 'Sure thing:\n```json\n{"probability": 0.3, "reasoning": "hedge"}\n```\nDone.'

    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope(text))

    resp = call_claude("p", runner=runner)
    assert resp.ok is True
    assert resp.parsed == {"probability": 0.3, "reasoning": "hedge"}


def test_first_balanced_brace_region_is_extracted_when_not_pure_json():
    text = 'Here is my answer: {"probability": 0.7, "reasoning": "vol rich"} — hope that helps!'

    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope(text))

    resp = call_claude("p", runner=runner)
    assert resp.ok is True
    assert resp.parsed == {"probability": 0.7, "reasoning": "vol rich"}


def test_unparseable_result_returns_ok_false_and_preserves_raw_text():
    text = "I refuse to answer in JSON today."

    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope(text))

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert resp.parsed is None
    assert resp.text == text
    assert resp.error != ""


def test_nonzero_exit_code_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(returncode=1, stdout="", stderr="rate limited")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert "rate limited" in resp.error


def test_timeout_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert "timeout" in resp.error.lower()


def test_arbitrary_runner_exception_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        raise OSError("claude binary not found")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert "claude binary not found" in resp.error


def test_invalid_cli_envelope_json_returns_ok_false():
    def runner(cmd, **kwargs):
        return _completed(stdout="not even json")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert resp.parsed is None


# ---- CRITICAL 1: valid JSON that is not an object must never reach a caller ----
#
# `parsed` is typed `dict | None`, and every call site does `parsed.get(...)`
# or `"key" in parsed`. A bare `"ABSTAIN"`, a top-level list, a number or a
# bool are all valid JSON and all natural model replies (the trader prompt
# literally offers "the literal string ABSTAIN"), so `_extract_json` must
# never hand back a non-dict.

def _resp_for(result_text):
    def runner(cmd, **kwargs):
        return _completed(stdout=_envelope(result_text))
    return call_claude("p", runner=runner)


def test_bare_json_string_reply_is_not_a_dict_and_abstains():
    resp = _resp_for('"ABSTAIN"')
    assert resp.parsed is None
    assert resp.ok is False
    assert resp.text == '"ABSTAIN"'          # raw text preserved for replay
    assert resp.error != ""


def test_json_float_reply_is_not_a_dict_and_abstains():
    resp = _resp_for("0.62")
    assert resp.parsed is None
    assert resp.ok is False


def test_json_bool_reply_is_not_a_dict_and_abstains():
    resp = _resp_for("true")
    assert resp.parsed is None
    assert resp.ok is False


def test_json_list_of_scalars_reply_is_not_a_dict_and_abstains():
    resp = _resp_for('["c1", "c2"]')
    assert resp.parsed is None
    assert resp.ok is False


def test_fenced_json_list_of_scalars_abstains():
    resp = _resp_for('```json\n["c1", "c2"]\n```')
    assert resp.parsed is None
    assert resp.ok is False


def test_top_level_list_wrapping_one_object_falls_through_to_brace_tier():
    # Tier 2 yields a list (not a dict) so it must fall through to the
    # balanced-brace tier, which recovers the object the model meant. The
    # contract under test is only that `parsed` is a dict or None — never a
    # list that a caller's .get() would explode on.
    resp = _resp_for('[{"choice": "c1", "reasoning": "ok"}]')
    assert resp.parsed is None or isinstance(resp.parsed, dict)
    assert resp.parsed == {"choice": "c1", "reasoning": "ok"}


def test_fenced_list_wrapping_one_object_falls_through_to_brace_tier():
    resp = _resp_for('```json\n[{"choice": "c1", "reasoning": "ok"}]\n```')
    assert resp.parsed is None or isinstance(resp.parsed, dict)


def test_parsed_is_always_dict_or_none_across_every_measured_reply():
    for text in ('"ABSTAIN"', "0.62", "true", "null", "[]", '["c1"]',
                 '```json\n[1, 2]\n```', '{"choice": "c1"}'):
        resp = _resp_for(text)
        assert resp.parsed is None or isinstance(resp.parsed, dict), text
        if resp.ok:
            assert isinstance(resp.parsed, dict), text


# ---- CRITICAL 2: call_claude is total — the envelope can be anything ----

def test_non_dict_envelope_list_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(stdout="[]")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert resp.parsed is None
    assert resp.error != ""


def test_non_dict_envelope_number_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(stdout="5")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False


def test_non_dict_envelope_string_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(stdout='"hello"')

    resp = call_claude("p", runner=runner)
    assert resp.ok is False


def test_non_dict_envelope_null_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(stdout="null")

    resp = call_claude("p", runner=runner)
    assert resp.ok is False


def test_non_numeric_cost_is_coerced_not_raised():
    def runner(cmd, **kwargs):
        return _completed(stdout=json.dumps(
            {"result": '{"probability": 0.5}', "total_cost_usd": "abc"}))

    resp = call_claude("p", runner=runner)
    assert resp.ok is True                 # a bad cost field is not a bad answer
    assert resp.cost_usd == 0.0
    assert resp.parsed == {"probability": 0.5}


def test_non_string_result_field_returns_ok_false_never_raises():
    def runner(cmd, **kwargs):
        return _completed(stdout=json.dumps(
            {"result": {"probability": 0.5}, "total_cost_usd": 0.01}))

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert resp.parsed is None


def test_runner_returning_a_junk_object_returns_ok_false_never_raises():
    class Junk:
        pass

    def runner(cmd, **kwargs):
        return Junk()          # no .returncode, no .stdout

    resp = call_claude("p", runner=runner)
    assert resp.ok is False
    assert resp.error != ""


def test_tools_flag_disabled_and_model_passed_through():
    captured = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout=_envelope('{"x": 1}'))

    call_claude("p", model="claude-sonnet-5", runner=runner)
    cmd = captured["cmd"]
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--disable-slash-commands" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
