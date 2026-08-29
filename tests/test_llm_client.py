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
