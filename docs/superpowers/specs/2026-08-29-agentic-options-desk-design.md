# Design — The Options Desk That Grades Itself

Date: 2026-08-29
Status: approved, pending implementation plan
Scope: Phases 3–7 of PLAN.md (the agentic layer, execution, judging surface)

---

## 1. Problem

After Phases 1–2 the repo contains sound deterministic infrastructure — data
adapter, candidate builder, risk guard, hash-chained journal, walk-forward
engine, 212 passing tests — and **no agency whatsoever**. The inherited LLM
machinery (`opus_analyst.py`) is a single-shot classifier: it receives a
pre-rendered string, returns one JSON verdict, has no tools, no memory, and no
multi-step reasoning. Its system prompt still describes a *cryptocurrency*
analyst and cites an unearned "1006 trade analyses".

This design specifies the agentic layer, the execution path, and the judging
surface, under a hard 2026-09-04 15:00 UTC deadline.

## 2. Goals and non-goals

**Goals**
1. Genuine multi-agent reasoning inside deterministic rails.
2. A self-grading loop that measurably changes future decisions.
3. A credential-free judge experience that replays real decisions.
4. Demonstrable compliance with the hackathon's Alpaca requirements.
5. A real *filled* multi-leg paper options order.

**Non-goals (explicitly cut)**
- Tool-calling analysts. Rejected: breaks prompt hashing, kills deterministic
  replay, and restores the 30k-token/call overhead. Data is fetched
  deterministically *before* any LLM runs.
- Chart/vision analysis (`chart_analyzer.py`). Expensive, low signal, already
  disabled.
- LLM-driven universe selection. Latency with no judge-visible benefit.
- LangGraph or any agent framework. Subprocess calls returning dataclasses need
  none of it.
- On-chain anything. Removed in Phase 1, stays removed.

## 3. Constraints

**Project hard rules (CLAUDE.md).** LLMs never invent strikes/quantities/order
parameters; every order passes RiskGuard; defined-risk structures only; ABSTAIN
is first-class; every decision is journalled with a prev-hash chain; kill switch
halts everything; paper account only.

**Amended rule — the dual-model veto.** CLAUDE.md requires "two different model
families must agree". Only the `claude` CLI is available (no API keys, no
Ollama). Two Claude calls on the same context correlate heavily and would
produce a veto that looks rigorous and is not. The rule is therefore restated
as **two decorrelated reviewers**:
1. a deterministic thesis-consistency check in pure code, and
2. a *blind* Claude call that sees the candidate and price action but never the
   committee's reasoning.

This is a deliberate, documented deviation, approved 2026-08-29.

**Hackathon rules.** Must use Alpaca Trading API **plus** the MCP server or the
CLI. Strategy must involve options. New dedicated paper account at $100k.
Submissions close 2026-09-04 15:00 UTC; judging begins immediately, so nothing
can be fixed afterwards.

**Broker constraints (verified).** Options **level 3** required for spreads.
Multi-leg: 2–4 legs, unique symbols, `time_in_force=day` only, no top-level
`symbol`/`side`, `limit_price` positive = debit and negative = credit — closing
a debit spread therefore requires a *negative* limit price.

**LLM budget (measured).** `claude -p --tools "" --strict-mcp-config` costs
~$0.019/call on Haiku and ~$0.033 on Sonnet 5, at 6–9s latency, versus $0.303
naive. Committee target: ≤ $0.15/decision.

## 4. Architecture

```
                    ┌─── deterministic ───┐
alpaca_data ──► candidate_builder ──► TradeIntent[] (ids c1..cN)
                                          │
                                render_snapshot()   ← ONE hashable string
                                          │
        ┌─────────────── committee (parallel) ──────────────┐
        │  vol_analyst   structure_analyst   bear_adversary │  Haiku
        │  each → AnalystView{p, abstained, reasoning}      │
        └───────────────────────┬───────────────────────────┘
                                │  one debate round (bull ↔ bear)
                                ▼
                    trader (Sonnet) → candidate_id | ABSTAIN
                                ▼
              ┌──── veto layer (both must pass) ────┐
              │ ① thesis_check()   pure code        │
              │ ② blind_review()   Haiku, starved   │
              └──────────────┬──────────────────────┘
                             ▼
                       RiskGuard  ──► ALLOW | DENY | DOWNSIZE
                             ▼
                    pre_mortem() → ExitTrigger[]
                             ▼
                    executor (Alpaca CLI adapter)
                             ▼
                    journal.append(...)  every step
                             ▼
                  calibration → analyst weights → next cycle
```

### 4.1 Components

| Module | Purpose | Depends on |
|---|---|---|
| `llm/client.py` | `claude -p` subprocess wrapper: cost-optimised flags, timeout, 3-tier JSON extraction, prompt cache | — |
| `llm/cache.py` | `sha256(role,prompt)` → JSON file with prompt + raw response. Cache **and** audit **and** replay corpus | — |
| `committee/snapshot.py` | Renders market state + candidates into one deterministic string | `alpaca_data`, `analytics`, `candidate_builder` |
| `committee/analysts.py` | Vol / Structure / Bear roles; each returns `AnalystView` | `llm`, `snapshot` |
| `committee/debate.py` | One bull↔bear round; transcript as plain concatenated string | `analysts` |
| `committee/trader.py` | Picks one candidate id or `"ABSTAIN"` (typed as a `Literal` over this cycle's ids), validated against the live id set | `debate` |
| `committee/veto.py` | `thesis_check()` (code) + `blind_review()` (LLM) | `analytics`, `llm` |
| `committee/premortem.py` | Failure statements → `ExitTrigger[]` (machine-checkable) | `llm` |
| `calibration.py` | Brier score per analyst over resolved outcomes → weights | `journal` |
| `alpaca_cli.py` | Subprocess adapter over the official Alpaca CLI | — |
| `executor_options.py` | Builds/sends atomic multi-leg orders; monitors exits | `alpaca_cli`, `risk_guard` |
| `dashboard/` + `judge/` | Live desk and credential-free replay page | `journal` |

Each is a small single-purpose module with a dataclass interface, per CLAUDE.md.

### 4.2 Key data structures

```python
@dataclass(frozen=True)
class AnalystView:
    role: str
    probability: float       # P(this trade is profitable), 0..1
    abstained: bool
    abstain_reason: str
    reasoning: str
    model: str
    prompt_hash: str         # links to the cached prompt/response artifact

@dataclass(frozen=True)
class ExitTrigger:
    kind: str                # "underlying_beyond" | "iv_spike" | "dte_below" | "credit_decay"
    threshold: float
    rationale: str           # the pre-mortem sentence that produced it

@dataclass(frozen=True)
class CycleRecord:
    """The single serialized truth for one decision. Nothing lives elsewhere."""
    cycle_id: str
    snapshot_hash: str
    candidates: tuple[TradeIntent, ...]
    views: tuple[AnalystView, ...]
    debate_transcript: str
    chosen: str              # candidate id or "ABSTAIN"
    veto: dict               # {thesis_ok, blind_ok, reason}
    guard: dict              # {decision, reason, approved_contracts}
    exits: tuple[ExitTrigger, ...]
    decision_hash: str       # sha256(stable_json(record without this field))
```

### 4.3 Aggregation rules

- **Abstention contract.** An abstaining analyst is excluded from both the
  numerator *and* the denominator of the weighted probability. A genuine 0.5 is
  a view; an abstention is not. "No opinion must not masquerade as neutral."
- **Calibration weights.** Each analyst's weight is a function of its Brier
  score over resolved predictions. Weight floors at a small non-zero value so a
  demoted analyst still speaks but barely votes. Weights are recomputed from the
  journal, never stored as mutable state.
- **Code renders the header, code parses the header.** The model's prose is
  never regexed for a decision; the parsed value is re-rendered by code and read
  back.
- **Candidate ids are validated against the set generated this cycle.** A
  hallucinated id is treated as ABSTAIN, not as an error to paper over.

### 4.4 Failure handling

The governing distinction, adopted from `ai-hedge-fund`:

> **Fail loud on data. Fail soft on the LLM.**

- A failed/empty data fetch **raises**. It must never become a confident
  neutral view. (`alpaca_data` already returns empty and logs; the committee
  treats empty as a hard stop for that underlying.)
- A failed LLM call, timeout, or unparseable response **abstains**, with
  `abstain_reason` recorded.
- If every analyst abstains, the cycle ABSTAINs and says why.
- Any RiskGuard exception is already DENY (built, mutation-tested).
- Rate-limit exhaustion on the Claude subscription degrades to ABSTAIN, so a
  live demo stalls into "no trade, here's why" rather than crashing.

### 4.5 Compliance path

Primary order path is the **official Alpaca CLI** wrapped in a subprocess
adapter (`alpaca_cli.py`): `account get`, `position list`, `order get`, and
order submission via `alpaca api POST /v2/orders` carrying the multi-leg
payload. This is the pattern the strongest same-event competitor shipped.

Additionally a committed `.mcp.json` declares `uvx alpaca-mcp-server` with
read-only toolsets (`account,options-data,stock-data`), used for a real chain
lookup demonstrated in the video. This satisfies "Trading API **plus** MCP
server **or** CLI" by both routes.

`alpaca-py` is retained for analysis and backtesting, where the CLI is awkward.

### 4.6 Judge surface

Static HTML, no auth, no LLM in the judge path — which is exactly what makes it
replay perfectly. Information architecture, top to bottom:

1. Five-step trace rail: Snapshot → Committee → Veto → Guard → Execution.
2. Scenario selector with a verdict status pill.
3. Six KPI cards (scenario, verdict + reason code, executable y/n, journal
   chain status, requested vs approved size, structure).
4. A "why this is not fully green" card, **hidden when green**, auto-generated
   from the record — this converts a refusal into a demonstrated capability.
5. Five numbered JSON panels matching the rail.
6. Explicit boundary notes: paper only, demo fixtures, what is *not* claimed.

Four committed scenarios as golden files: **ALLOW · DENY · DOWNSIZE ·
FAIL-CLOSED**. Two of four are refusals, deliberately.

## 5. Testing strategy

- TDD throughout, as in Phases 1–2.
- LLM-dependent modules are tested against **recorded** responses from the
  prompt cache — no network in the test suite.
- Golden-file tests: replaying a committed scenario must produce a
  byte-identical `decision_hash`.
- Mutation-check the veto layer as was done for RiskGuard.
- Target: 260+ tests, suite runtime under ~90s, zero warnings.

## 6. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Claude rate limits during live session | Medium | Degrade to ABSTAIN; pre-record a demo cycle as backup footage |
| Multi-leg order rejected (level/buying power) | Medium | Startup check gates level ≥ 3; log full broker rejection text; test with a 1-contract minimum first |
| No fill in market hours | Medium | Marketable limit at mid, then re-price once; accept ABSTAIN over chasing |
| Committee too slow for a 30-min cycle | Low | Analysts run in parallel; measured 6–9s each |
| Over-ambition sinks the submission | **High** | Every day ends shippable; see schedule |

## 7. Schedule (each day ends with a working submission)

| Day | Ships | If it slips |
|---|---|---|
| Sun 30 Aug | CLI adapter + `.mcp.json` + options executor → **a real filled paper spread** | Compliance alone de-risks the entry |
| Mon 31 Aug | Committee + debate + dual veto + pre-mortem; live session #1 | Executor already trades deterministically |
| Tue 1 Sep | Calibration seeded by walk-forward replay; dashboard | Loop works unseeded |
| Wed 2 Sep | `/judge` page, golden scenarios, README, Docker, CI | Dashboard alone demos |
| Thu 3 Sep | Live session #2 for footage; video; PDF | — |
| Fri 4 Sep | Flatten, verify journal, submit by 12:00 UTC (3h buffer) | — |

## 8. Prompt rewrite

`ai_prompts.py`'s crypto analyst prompt is **deleted, not adapted**, along with
its unearned "1006 trade analyses" claim. Replacement prompts are written for
defined-risk equity options: IV rank and term structure, realized-vs-implied
spread, liquidity (OI, quote width), assignment and early-exercise risk, and
event calendar. Each analyst prompt states its role, its evidence, and that its
only outputs are a probability or an abstention.

## 9. Open questions

None blocking. Deferred: whether calibration weights should decay with age
(revisit once there are ≥ 50 resolved predictions).
