# Committee v1 — build report

Scope: `llm/client.py`, `llm/cache.py`, `committee/snapshot.py`,
`committee/analysts.py`, `committee/trader.py`, `committee/veto.py`, per
`docs/superpowers/specs/2026-08-29-agentic-options-desk-design.md` §3, 4.1-4.4
and Addendum A. `committee/debate.py`, `committee/premortem.py`,
`calibration.py`, and the executor wiring are out of this task's scope
(later phases) and were not touched.

## What was built

1. **`llm/client.py`** — `LLMResponse` (frozen dataclass) + `call_claude()`.
   Subprocess wrapper for `claude -p ... --tools "" --strict-mcp-config
   --disable-slash-commands --output-format json`. Three-tier JSON
   extraction (fenced ```json, whole-string, first balanced-brace region).
   `runner` injected (defaults to `subprocess.run`). **Never raises** — every
   failure mode (timeout, non-zero exit, bad envelope, unparseable result
   text) returns `LLMResponse(ok=False, ...)` with the raw text preserved
   and the error recorded. `prompt_hash = sha256(model + "\n" + prompt)`.

2. **`llm/cache.py`** — `PromptCache(cache_dir)` with `get`/`put`, one JSON
   file per `prompt_hash`. Docstring states its triple role explicitly: cost
   saver, audit record, deterministic-replay corpus. Failed parses are
   persisted with their error so the raw response survives for debugging.

3. **`committee/snapshot.py`** — `render_snapshot(underlying, spot,
   realized_vol, candidates, max_candidates=12)`. Sorts candidates by a
   canonical key (structure, dte, strikes, net credit) before assigning ids
   `c1..cN` and capping, so the same set of inputs renders byte-identical
   output regardless of upstream ordering. No timestamps; fixed-precision
   float formatting.

4. **`committee/analysts.py`** — `AnalystView` dataclass; `vol_analyst` and
   `bear_adversary` (both Haiku). New prompts written from scratch for
   defined-risk US-equity options (IV rank, realized-vs-implied spread, term
   structure, liquidity/OI/quote-width, assignment risk, event calendar) —
   the old `ai_prompts.py` crypto prompt was neither copied nor imported.
   `aggregate(views)` excludes abstaining analysts from both numerator and
   denominator; returns `None` if every analyst abstained.

5. **`committee/trader.py`** — `choose(snapshot, views, candidate_ids,
   client=None)` on `claude-sonnet-5`. Returned id is validated against
   `candidate_ids`; a hallucinated/malformed id or a missing `candidate_ids`
   list becomes `("ABSTAIN", reason)`, never an exception.

6. **`committee/veto.py`** — `thesis_check(intent, spot)` (pure code, no
   LLM): uses `analytics.position_greeks`, checks bull-put-spread → net long
   delta, bear-call-spread → net short delta, `{iron_condor, long_straddle}`
   → `|delta| <= 15` (half of risk.yaml's 30-delta guard limit — documented
   heuristic for "near neutral"). Fails closed on `None` Greeks or an
   unrecognized structure. `blind_review(intent, spot, realized_vol,
   client=None)` (Haiku): prompt carries only the candidate's own legs/
   credit/loss/breakevens/DTE and price/vol context — no committee reasoning
   text — so the two reviewers are decorrelated per the design spec's
   amended dual-veto rule. LLM failure or a missing/non-bool `agree` field
   vetoes (fails closed).

## TDD evidence

Each module: test file written first, run to confirm `ModuleNotFoundError`
(right-reason failure), then implementation added, then rerun to green.
Example transcript for `llm/client.py`:

```
$ python3 -m pytest tests/test_llm_client.py -q
ModuleNotFoundError: No module named 'llm.client'
... (implemented llm/client.py) ...
$ python3 -m pytest tests/test_llm_client.py -q
10 passed in 0.01s
```

Same red→green cycle for cache, snapshot, analysts, trader, veto. One real
within-cycle regression: `test_snapshot.py`'s first draft used a long-leg
quote (bid 1.40/ask 1.60) whose spread (13.3%) failed `candidate_builder`'s
own 10%-of-mid liquidity gate, so `build_bull_put_spread` returned `None`
and every snapshot test failed with `AttributeError: 'NoneType' object has
no attribute 'legs'` — caught immediately by running the suite, fixed by
tightening the test fixture's spread (bid 1.45/ask 1.55), not by touching
`committee/snapshot.py`.

## Test results

- Baseline before this work: 335 passed.
- After: **389 passed**, 0 failed, 0 warnings, ~55s (`python3 -m pytest
  tests/ -q`).
- New tests: 10 (`test_llm_client.py`) + 5 (`test_llm_cache.py`) + 8
  (`test_snapshot.py`) + 12 (`test_analysts.py`) + 8 (`test_trader.py`) + 11
  (`test_veto.py`) = 54.
- No network in the suite — every LLM call is through an injected fake
  `runner`/`client`. Confirmed by grepping the new test files for
  `subprocess`/network calls: none outside the two ad-hoc sanity checks
  below, which were run manually, not as part of `pytest`.
- Abstain-path coverage (explicitly, per instructions): LLM timeout/non-zero
  exit/malformed envelope (client), explicit model abstain, out-of-range /
  missing / non-numeric probability, all-analysts-abstain (analysts),
  hallucinated id / malformed id / missing choice / empty candidate set
  (trader), unmeasurable Greeks / unknown structure / LLM failure / malformed
  `agree` field (veto).

## Real `claude -p` sanity checks (2 calls made, as permitted)

Both went through the actual implementation (`llm.client.call_claude`), not
a fake, to confirm the subprocess/JSON-extraction path works against the
real CLI.

| Model | ok | parsed correctly | cost_usd | latency |
|---|---|---|---|---|
| claude-haiku-4-5 | True | yes | $0.0237 | 9.07s |
| claude-sonnet-5 | True | yes | $0.0653 | 24.42s |

The haiku call landed close to the brief's measured figures (~$0.019,
~6-9s). The sonnet call cost ~2x and took ~4x the brief's figures
(~$0.033, ~6s) — plausibly session/load variance rather than a client bug,
since the envelope parsed correctly and `total_cost_usd` came straight from
the CLI's own JSON. Also notable: given a prompt that explicitly modeled
"respond with `{"choice": "c1", ...}`", the model reasoned independently and
chose `ABSTAIN` instead while echoing the literal reasoning string back —
i.e. it treated the example as instructive framing, not a command to parrot,
which is reassuring behavior for `trader.choose`'s real-world use but is a
reminder that these two data points are not a statistically meaningful
latency/cost benchmark, just a wiring sanity check. No committee code
depends on the sonnet call succeeding within any particular latency; a
timeout still resolves to ABSTAIN.

## Files changed

New:
- `llm/__init__.py`, `llm/client.py`, `llm/cache.py`
- `committee/__init__.py`, `committee/snapshot.py`, `committee/analysts.py`,
  `committee/trader.py`, `committee/veto.py`
- `tests/test_llm_client.py`, `tests/test_llm_cache.py`,
  `tests/test_snapshot.py`, `tests/test_analysts.py`, `tests/test_trader.py`,
  `tests/test_veto.py`

No existing file was modified — `candidate_builder.py`, `analytics.py`, and
`journal.py` were read-only dependencies, per the task's instructions.

Commits (5, one per module/pairing):
1. `committee v1: llm/client.py + llm/cache.py`
2. `committee v1: committee/snapshot.py`
3. `committee v1: committee/analysts.py`
4. `committee v1: committee/trader.py`
5. `committee v1: committee/veto.py`

## Self-review findings

- **Cache is unwired.** `llm/cache.py` and `llm/client.py` are independent
  and separately tested, per the module split in the spec table. Nothing in
  `committee/analysts.py`, `trader.py`, or `veto.py` calls
  `PromptCache.get`/`put` around its `client(...)` invocation — that
  wiring (check cache before spending a call, write the record after)
  wasn't in this task's explicit module list and would naturally belong to
  an orchestrating layer (a `committee/session.py`-shaped module, or
  `committee/debate.py`, per the architecture diagram) that doesn't exist
  yet. Flagging so it isn't mistaken for "already integrated."
- **`NEUTRAL_DELTA_THRESHOLD = 15.0`** in `veto.py` is a documented
  heuristic (half of risk.yaml's `|net delta| <= 30`), not a value derived
  from the spec, which only says "near delta-neutral" without a number.
  Reasonable but worth a second look once real chain data is flowing.
- **`aggregate()` is equal-weighted**, not weighted by calibration score —
  correct for this phase since `calibration.py` (Brier-based weights) is
  explicitly a later-phase module per PLAN.md/the spec; the function's
  docstring says so and the signature (`list[AnalystView]` in, `float |
  None` out) won't need to change when weights land.
- **`committee/trader.py`'s prompt includes the raw candidate ids** but not
  full candidate detail beyond what's already in the snapshot string passed
  alongside — this matches the "code renders the header, code parses the
  header" principle (spec 4.3): the trader never sees anything the snapshot
  didn't already deterministically render.
- Ran `python3 -m py_compile` on all six new modules — no syntax issues.

## Not done (explicitly out of scope for this task)

- `committee/debate.py` (bull↔bear round), `committee/premortem.py`,
  `calibration.py`, executor/journal wiring, and cache integration into the
  committee call sites — all later-phase pieces per PLAN.md Phase 3/4 and
  the architecture diagram, not part of the six modules requested here.
