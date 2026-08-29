"""Subprocess wrapper around the `claude` CLI.

The only LLM available in this environment is a subscription-backed `claude`
CLI call — no API key, no Ollama. Measured cost: naive `claude -p` runs
~30,212 cache tokens and $0.303/call; adding `--tools ""` (disabling all
tool use) cuts that ~16x to ~7,600 cache tokens and ~$0.019/call on
claude-haiku-4-5 (~$0.033 on claude-sonnet-5), at 6-9s latency. That flag
must never be dropped.

`call_claude` NEVER raises. Any subprocess failure, non-zero exit, timeout,
or unparseable envelope/response is folded into `LLMResponse(ok=False, ...)`.
This is deliberate: per CLAUDE.md and the design spec ("fail loud on data,
fail soft on the LLM"), a broken LLM call must make its caller abstain, not
crash a scan that may be mid-cycle across several underlyings. `runner` is
injected (defaults to `subprocess.run`) so the whole call path is testable
without touching a network or a real binary.
"""
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass

DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class LLMResponse:
    """The outcome of one `claude -p` call.

    `text` always holds the model's raw result text when it was obtainable
    (even if `parsed` extraction failed), so a bad response can be inspected
    and replayed rather than discarded silently.
    """
    ok: bool
    text: str
    parsed: dict | None
    model: str
    prompt_hash: str
    error: str
    cost_usd: float


def _prompt_hash(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def _extract_first_balanced_braces(text: str) -> str | None:
    """Return the first top-level {...} region, tolerating a brace inside a
    string literal by tracking quote state (good enough for LLM prose that
    wraps a JSON object in a sentence)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(text: str) -> dict | None:
    """Three-tier JSON extraction from an LLM's free-form response text.

    Tried in order: (a) a ```json fenced block — Haiku's common style,
    (b) the whole string parsed as-is — Sonnet's common style,
    (c) the first balanced-brace region — for prose that wraps a JSON object
    in a sentence. Returns None, never raises, if nothing parses.
    """
    fence = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    region = _extract_first_balanced_braces(text)
    if region is not None:
        try:
            return json.loads(region)
        except json.JSONDecodeError:
            pass

    return None


def call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 120,
    runner=subprocess.run,
) -> LLMResponse:
    """Invoke the claude CLI with cost-optimised flags and parse its output.

    Never raises — see module docstring.
    """
    prompt_hash = _prompt_hash(model, prompt)
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format", "json",
    ]

    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return LLMResponse(
            ok=False, text="", parsed=None, model=model, prompt_hash=prompt_hash,
            error=f"claude CLI timeout after {timeout}s", cost_usd=0.0,
        )
    except Exception as e:  # noqa: BLE001 - any spawn failure must abstain, not raise
        return LLMResponse(
            ok=False, text="", parsed=None, model=model, prompt_hash=prompt_hash,
            error=f"claude CLI subprocess error: {e}", cost_usd=0.0,
        )

    if proc.returncode != 0:
        return LLMResponse(
            ok=False, text=proc.stdout or "", parsed=None, model=model,
            prompt_hash=prompt_hash,
            error=f"claude CLI exit {proc.returncode}: {(proc.stderr or '').strip()}",
            cost_usd=0.0,
        )

    stdout = proc.stdout or ""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        return LLMResponse(
            ok=False, text=stdout, parsed=None, model=model, prompt_hash=prompt_hash,
            error=f"invalid claude CLI envelope JSON: {e}", cost_usd=0.0,
        )

    result_text = envelope.get("result", "")
    cost_usd = float(envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0)

    parsed = _extract_json(result_text)
    if parsed is None:
        return LLMResponse(
            ok=False, text=result_text, parsed=None, model=model,
            prompt_hash=prompt_hash,
            error="could not extract JSON from claude response text",
            cost_usd=cost_usd,
        )

    return LLMResponse(
        ok=True, text=result_text, parsed=parsed, model=model,
        prompt_hash=prompt_hash, error="", cost_usd=cost_usd,
    )
