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
import math
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


def prompt_hash(model: str, prompt: str) -> str:
    """The cache key for one (model, prompt) pair.

    Public because the prompt cache lives outside this module: `committee`
    computes this key BEFORE deciding whether to spend a call, so a hit never
    reaches `call_claude` at all. Two callers computing the key two ways would
    silently halve the hit rate and break replay, so there is exactly one
    definition and it is this one.
    """
    return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


# Retained for the existing internal call sites and tests.
_prompt_hash = prompt_hash


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


def _loads_dict(raw: str) -> dict | None:
    """`json.loads` that yields a dict or nothing at all.

    Valid JSON that is not an object — a bare `"ABSTAIN"`, `0.62`, `true`,
    `null`, a top-level list — is NOT an answer this codebase can consume:
    every caller does `parsed.get(...)` or `"key" in parsed`. Returning such
    a value would turn a plausible model reply into an AttributeError /
    TypeError that crashes a whole scan cycle instead of abstaining.
    """
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        try:
            value = json.loads(_strip_trailing_commas(raw))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _strip_trailing_commas(raw: str) -> str:
    """Remove `,` that immediately precedes a closing `}` or `]`.

    Models emit these routinely and strict JSON rejects them. On 2026-09-01 a
    live trader call returned a complete decision — choice c2, with reasoning
    that engaged with the adversary's objection — and the desk discarded it
    and abstained, because of the comma in:

        {"choice": "c2", "reasoning": "...", }

    Scanned character by character while tracking string state, so a comma
    inside a string value is never touched: reasoning text routinely contains
    commas followed by braces, and a blind regex would corrupt the content it
    is meant to rescue. Only structural commas are removed, so genuinely
    malformed output still fails to parse and the caller still abstains.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch in "}]":
            # Drop whitespace back to, and including, a trailing comma.
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j:]
        out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> dict | None:
    """Three-tier JSON **object** extraction from an LLM's response text.

    Tried in order: (a) a ```json fenced block — Haiku's common style,
    (b) the whole string parsed as-is — Sonnet's common style,
    (c) the first balanced-brace region — for prose that wraps a JSON object
    in a sentence. A tier that parses to something other than a dict does not
    win: it falls through to the next tier (so `[{"choice": "c1"}]` is still
    recovered by tier (c)). Returns None, never raises, if no tier yields a
    dict — the caller then reports ok=False and its own caller abstains.
    """
    if not isinstance(text, str):
        return None

    fence = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if fence:
        parsed = _loads_dict(fence.group(1).strip())
        if parsed is not None:
            return parsed

    parsed = _loads_dict(text.strip())
    if parsed is not None:
        return parsed

    region = _extract_first_balanced_braces(text)
    if region is not None:
        parsed = _loads_dict(region)
        if parsed is not None:
            return parsed

    return None


def _float_or_zero(value) -> float:
    """Cost is telemetry, not an answer. A CLI that reports `"abc"` must not
    turn a good response into a raised ValueError."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 120,
    runner=subprocess.run,
) -> LLMResponse:
    """Invoke the claude CLI with cost-optimised flags and parse its output.

    Never raises — see module docstring. The whole body is wrapped: even a
    bug in this function, or a runner that returns something that is not a
    CompletedProcess, becomes ok=False rather than an exception escaping into
    a mid-cycle scan.
    """
    prompt_hash = _prompt_hash(str(model), str(prompt))
    try:
        return _call_claude_inner(prompt, model, timeout, runner, prompt_hash)
    except Exception as e:  # noqa: BLE001 - totality is the contract
        return LLMResponse(
            ok=False, text="", parsed=None, model=model, prompt_hash=prompt_hash,
            error=f"claude CLI wrapper error: {type(e).__name__}: {e}",
            cost_usd=0.0,
        )


def _call_claude_inner(prompt, model, timeout, runner, prompt_hash) -> LLMResponse:
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        # Load NONE of user/project/local settings. Without this the desk's
        # analysts inherit whatever skills, hooks and plugins happen to be
        # installed on the machine running the session. On 2026-08-31 a live
        # cycle abstained because a user-level plugin tells the model to look
        # for relevant skills before answering, and vol_analyst obeyed:
        #     I need to check for relevant skills before proceeding...
        #     <tool_call>{"type":"skillTool","skillName":"available"}</tool_call>
        # It spent its one turn on that and returned no probability, so the
        # cycle had no second view and abstained. `--disable-slash-commands`
        # does not help: the instruction arrives through a hook. `--bare`
        # also isolates but strips credentials, so the CLI replies
        # "Not logged in". Measured effect: 10,907 -> 6,778 prompt tokens,
        # the difference being the injected preamble.
        "--setting-sources", "",
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

    returncode = getattr(proc, "returncode", None)
    if returncode != 0:
        return LLMResponse(
            ok=False, text=str(getattr(proc, "stdout", "") or ""), parsed=None,
            model=model, prompt_hash=prompt_hash,
            error=f"claude CLI exit {returncode}: "
                  f"{str(getattr(proc, 'stderr', '') or '').strip()}",
            cost_usd=0.0,
        )

    stdout = getattr(proc, "stdout", "") or ""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return LLMResponse(
            ok=False, text=str(stdout), parsed=None, model=model,
            prompt_hash=prompt_hash,
            error=f"invalid claude CLI envelope JSON: {e}", cost_usd=0.0,
        )

    # The envelope itself is model-adjacent output: `[]`, `5`, `"hello"` and
    # `null` are all valid JSON that `.get` would explode on.
    if not isinstance(envelope, dict):
        return LLMResponse(
            ok=False, text=str(stdout), parsed=None, model=model,
            prompt_hash=prompt_hash,
            error=f"claude CLI envelope is {type(envelope).__name__}, expected object",
            cost_usd=0.0,
        )

    cost_usd = _float_or_zero(
        envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0)

    result_text = envelope.get("result", "")
    if not isinstance(result_text, str):
        return LLMResponse(
            ok=False, text="", parsed=None, model=model, prompt_hash=prompt_hash,
            error=f"claude CLI envelope 'result' is {type(result_text).__name__}, "
                  f"expected string",
            cost_usd=cost_usd,
        )

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
