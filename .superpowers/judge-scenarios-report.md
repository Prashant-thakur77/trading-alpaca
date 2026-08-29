# Judge replay scenarios — build report

## What was built

- `judge/scenarios/{allow,deny,downsize,fail_closed}.json` — four committed golden
  fixtures, each a complete decision trace: market snapshot, the full 12-candidate
  menu, both analyst views, the trader's choice + reasoning, both veto results, the
  guard verdict, and the resulting order payload (or `null` for a refusal).
- `scripts/replay.py` — the replay engine. `--list`, `--scenario NAME`, `--all`,
  `--verify`, `--json`. Re-runs only the deterministic half of a cycle
  (`committee.veto.thesis_check`, `risk_guard.RiskGuard.evaluate`,
  `options_orders.build_mleg_payload`) against each fixture's inputs, using the
  fixture's recorded analyst/trader/blind-veto output verbatim — it never calls an
  LLM. `fail_closed` replays by invoking the real `scripts/run_session.main` against
  injected fakes whose chain fetch raises, the same mechanism as
  `tests/test_run_session_main.py::TestDataFailures`.
- `tests/test_replay.py` — 27 tests: all four scenarios replay and match; `--verify`
  passes for all four; replaying twice is byte-identical; a fixture-driven replay
  works with `llm.client.call_claude`/`subprocess.run` patched to raise (proves zero
  LLM/network calls) and with every `ALPACA_*` env var unset (zero credentials); a
  corrupted/mismatched fixture raises `ScenarioError` (detected, not silently
  rendered) both at the API and CLI level; the CLI's `--list`/`--scenario`/`--all`/
  `--json`/`--verify` all exit correctly.
- `Makefile`: `make judge` → `python3 scripts/replay.py --all`.

## Provenance of each fixture

The desk's own `logs/prompt_cache/` held real recorded LLM responses from several
real `committee.decide.decide()` cycles run against a real SPY option chain on
2026-08-29 (spot $769.35) — more cycles than made it into `logs/journal.jsonl`
(which only captured the last, an all-abstain cycle). Three of those recorded
cycles had a trader pick a candidate that a real recorded blind-review call agreed
with (`agree: true`), giving three genuine trade candidates with full leg data
(strike/bid/ask/OI) recoverable from the analysts' and trader's own prompt text.

- **`allow.json`** — *mixed*: `vol_analyst`, `bear_adversary`, the trader's choice
  (`c3`, bear call spread 777/782, DTE 32) and the blind veto's `agree: true` are
  the real recorded LLM output from that cycle
  (`logs/prompt_cache/8beb8f40ae69...json` + 3 others). `thesis_check`, the guard
  verdict and the order payload are computed fresh from the real candidate,
  reconstructed via the real `candidate_builder.build_bear_call_spread`. The
  portfolio state (`open_positions=1`, headroom under every limit) is *constructed*
  — no live session has yet run a second concurrent position — chosen only to
  exercise an ordinary passing guard check on real guard code. Result: `ALLOW`.
- **`deny.json`** — *mixed*: a different real recorded cycle, trader picked `c4`
  (bear call spread 775/780, DTE 10), real `agree: true` blind veto. Portfolio state
  is *constructed* at `open_positions=3` — risk.yaml's `max_positions` cap — so the
  real guard code computes a genuine `DENY — At max_positions (3/3)` rather than a
  typed string.
- **`downsize.json`** — *mixed*: a third real recorded cycle, trader picked `c3`
  (bear call spread 781/786, DTE 32), real `agree: true` blind veto. The real
  committee always proposes 1 contract; this fixture *constructs* a request for 4 of
  the same real quotes, so 4 × $322 max loss = $1,288 exceeds risk.yaml's $1,000
  cap. The real guard computes `ALLOW_WITH_DOWNSIZE`, approving 3.
- **`fail_closed.json`** — *constructed*: no live cycle has hit a chain-fetch outage
  yet, so this fixture runs the real `scripts/run_session.main` (unmodified) with an
  injected data client whose `get_option_chain()` raises `ConnectionError`. The
  recorded stdout/exit code are the real, unedited output of that call: `DATA FETCH
  FAILED — option chain for SPY (...). This is an outage, not a market judgement. No
  trade.`, exit 1.

Each fixture's own `provenance` field spells this out per-piece (committee vs. veto
vs. portfolio-state vs. guard/payload) rather than one blanket label.

## TDD evidence

`tests/test_replay.py` was written and run first against a nonexistent
`scripts/replay.py` (`ModuleNotFoundError: No module named 'replay'` — collection
error, red) before any implementation existed. `scripts/replay.py` was then written
to satisfy it; first full run of the 27 tests passed 26/27 (missing `judge:`
Makefile target), fixed, then 27/27 green. Full suite: 583 passed (556 pre-existing
+ 27 new), no regressions, no weakened/deleted tests.

## `python3 scripts/replay.py --all` output

```
scenario: allow
provenance: mixed

1. SNAPSHOT
   SPY spot $769.35 — 12 candidate(s): c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12
2. COMMITTEE
   bear_adversary   p=0.43 — The +2.27% IV advantage is razor-thin...
   vol_analyst      p=0.80 — IV is +2.27pp above realized vol—options are rich...
   aggregate probability: 0.615
   trader: c3 — Bear call spread captures the rich IV-over-RV skew vol_analyst flags...
3. VETO
   thesis: PASS — net delta -8.72 consistent with bear call spread's bearish thesis
   blind:  PASS — Bear call spread is a sound defined-risk structure...
4. GUARD
   ALLOW — Within all limits: risk $288.50, 2/3 positions (1 contract(s) approved)
5. EXECUTION
{
  "order_class": "mleg", "type": "limit", "time_in_force": "day", "qty": "1",
  "limit_price": "-2.12", "client_order_id": "9a440cf2902b129a7ed6062faad4617e",
  "legs": [
    {"symbol": "SPY260930C00777000", "side": "sell", "ratio_qty": "1", "position_intent": "sell_to_open"},
    {"symbol": "SPY260930C00782000", "side": "buy", "ratio_qty": "1", "position_intent": "buy_to_open"}
  ]
}

REPLAY MATCHED the recorded outcome
------------------------------------------------------------
scenario: deny
provenance: mixed
...
4. GUARD
   DENY — At max_positions (3/3) (0 contract(s) approved)
5. EXECUTION
   no payload — the guard refused this candidate

REPLAY MATCHED the recorded outcome
------------------------------------------------------------
scenario: downsize
provenance: mixed
...
4. GUARD
   ALLOW_WITH_DOWNSIZE — max_loss $1,288.00 exceeds $1,000.00 (4->3 contracts) (3 contract(s) approved)
5. EXECUTION
{ "qty": "3", "limit_price": "-1.78", ... }

REPLAY MATCHED the recorded outcome
------------------------------------------------------------
scenario: fail_closed
provenance: constructed

1. SNAPSHOT
   FAILED (exit 1) — see injected_failure
   |   DATA FETCH FAILED — option chain for SPY (option chain request timed out).
   |   This is an outage, not a market judgement. No trade.
2. COMMITTEE
   not reached
3. VETO
   not reached
4. GUARD
   not reached
5. EXECUTION
   not reached

REPLAY MATCHED the recorded outcome
------------------------------------------------------------
```

Exit code of the `--all` run: 0 (all four matched). `make judge` runs the same
command. `python3 scripts/replay.py --verify` independently confirms all four:
`allow OK / deny OK / downsize OK / fail_closed OK`, exit 0.
