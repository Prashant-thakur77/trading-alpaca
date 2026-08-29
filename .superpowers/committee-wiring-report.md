# Committee wiring report — 2026-08-29

The agentic layer is now in the loop. `scripts/run_session.py` no longer picks a
candidate by a credit/max-loss ratio; it hands the deterministically-built
candidate set to the committee, trades only what the committee names, and still
puts that choice through RiskGuard.

Suite: **429 → 491 passing**, zero failures, zero skips, no network in the test
suite. Three commits.

| Commit | What |
|---|---|
| `e4c58d7` | `fix(snapshot,analysts)`: the IV-vs-realized sign convention |
| `d106722` | `feat(committee)`: `decide()` — orchestrator, journal trail, wired cache |
| `70b4224` | `feat(session)`: committee wired into `run_session`, `--no-llm` fallback |

---

## 1. What was built

### `committee/decide.py` — the orchestrator

`decide(underlying, spot, realized_vol, candidates, journal, cache=None,
client=call_claude) -> CommitteeDecision`

Flow: render the snapshot → run the two analysts **concurrently** → aggregate →
ask the trader → resolve the id through `snapshot.candidates` → run **both**
vetoes → return.

`CommitteeDecision` is frozen and carries `chosen`, `choice_id`, `views`,
`aggregate_probability`, `trader_reasoning`, `thesis_ok`, `thesis_reason`,
`blind_ok`, `blind_reason`, `snapshot_hash`, `abstain_reason`.

Two deliberate calls worth flagging for review:

- **`choice_id` is `"ABSTAIN"` whenever `chosen` is None**, including when a
  specific candidate was picked and then vetoed. `choice_id` names what the
  *desk* decided. Which candidate was vetoed is in `abstain_reason` and in the
  journal's `veto` entry. Conflating "the trader liked c3" with "the desk chose
  c3" is how a vetoed trade gets executed by a careless reader.
- **Both vetoes always run**, even when the thesis check has already failed. The
  judge page shows both verdicts, and a blank second opinion is
  indistinguishable from one that was skipped. Cost is one Haiku call.

`client` is a *factory* — `callable(prompt, model=...)` — not a pre-bound
client, because the roles deliberately run on different models (Haiku for the
analysts and blind reviewer, Sonnet for the trader) and a single bound client
would silently collapse that split.

### Journalling (hard rule 5)

One entry per stage, in order: `snapshot`, one `analyst_view` per analyst,
`trader_choice`, `veto`, `committee_decision`. Every append routes through
`committee.decide._record`, which logs at error level and swallows — the same
contract and the same rationale as `executor_options._record`. `journal=None`
is accepted and means "write nothing" (the dry-run case).

Every early exit goes through one local `abstain()` helper, so there is no path
that can return an abstention which was never written down.

### Prompt cache

Every LLM call goes through `_cached_client`, which keys on
`llm.client.prompt_hash(model, prompt)` — now a public function, because two
callers computing the key two ways would halve the hit rate and break replay.
`get` first, `put` after. `LLMResponse` has no `prompt` field, so the wrapper
supplies it; without it the cache would be a cost saver only, not an audit
record and not a replay corpus.

A cached **failure** replays as a failure. That is what makes a refusal cycle
replayable at all; the operational escape hatch for a live rate limit is
`--no-llm`, not a cache that quietly forgets. A record that cannot be
reconstructed (corrupt, older schema, non-dict `parsed`) is treated as a miss
and re-called — a half-understood record must never become a *confident*
answer.

### `run_session.py`

- `--no-llm` keeps the old deterministic selection (`best_by_credit_ratio`,
  extracted and named — it is a fallback now, not a placeholder). The active
  mode is printed every cycle.
- `--dry-run` prints the whole chain and passes `journal=None` to the
  committee: a rehearsal stays out of the judged chain, which is the rule
  `_abstain(dry_run=True)` already followed.
- An ABSTAIN exits 0 with its reason printed.
- A committee that raises is converted to an abstention by `run_committee`;
  there is no path from a committee failure to an order.
- The guard runs on whatever the committee chose, unchanged. The committee
  proposes; RiskGuard disposes.

---

## 2. TDD evidence

Every piece was written test-first and each red state was observed before the
implementation existed.

| Step | Red evidence | Green |
|---|---|---|
| Sign convention | `assert 'ABOVE' in 'IV_MINUS_REALIZED: +8.19pp'` → 2 failed, 19 passed | 43 passed |
| `decide()` | `ModuleNotFoundError: No module named 'committee.decide'` at collection | 38 passed |
| `run_session` wiring | 16 failed, 42 passed | 58 passed |

Full suite after each commit: 437 → 475 → 491, all green.

### Tests covering hard rule 1 (`tests/test_decide.py::TestHardRuleOneIdMapping`)

- `test_the_same_id_yields_the_same_intent_under_a_shuffled_input` — **the
  requested shuffle test**. The candidate list is reversed; the same id must
  yield the same intent. If the orchestrator resolved `"c1"` by indexing its
  own input list, a reordered chain fetch would send a *different trade than the
  id names* while the id validated cleanly and every guard reported PASS.
- `test_every_offered_id_resolves_to_its_own_snapshot_intent` — every id in the
  snapshot, against a reversed input list.
- `test_the_chosen_intent_is_the_snapshots_own_mapping_not_the_inputs`
- `test_a_hallucinated_id_abstains_rather_than_falling_back` — `"c99"` is an
  abstention, never "the closest one".
- `test_no_candidates_abstains_without_spending_a_single_llm_call`

Plus `run_session`'s `test_the_committees_choice_is_the_order_that_is_sent`,
which asserts the OCC symbols on the wire match the legs of the intent the
committee named — not the ratio-best one.

### Tests covering hard rule 5 (`tests/test_decide.py::TestHardRuleFiveJournal`)

- `test_a_traded_cycle_journals_every_stage_in_order` — exact sequence
  `["snapshot", "analyst_view", "analyst_view", "trader_choice", "veto",
  "committee_decision"]`.
- One test per entry type asserting its full payload.
- `test_an_abstaining_analyst_journals_a_null_probability_not_a_zero` — a 0.0
  would read as maximum bearishness, i.e. an abstention masquerading as the
  strongest possible opinion.
- `test_an_abstain_is_journalled_as_fully_as_a_trade` and
  `test_an_all_abstain_cycle_still_journals_a_decision`.
- `test_a_journal_failure_never_breaks_the_decision` — a journal whose every
  append raises `OSError`; the decision still returns `c1`, and all six appends
  were still attempted.
- `test_the_journal_chain_stays_verifiable` — `verify_chain` over the result.

---

## 3. Cache-replay proof

`test_a_second_identical_decide_makes_zero_llm_calls`: first `decide()` makes
exactly 4 client calls; a second `decide()` with a fresh client makes
`client.calls == []` and returns the same `choice_id`, `chosen`,
`aggregate_probability`, `trader_reasoning`, `thesis_ok`, `blind_ok` and
`snapshot_hash`. Supporting tests cover the record contents, the shared key
definition, cached failures, corrupt records re-calling, and a moved spot
correctly *not* reusing another cycle's answers.

**Verified live**, not only in tests. First real run: 3 `claude -p` calls
(the trader abstained, so the blind review never ran), `$0.136` total —
inside the spec's $0.15/decision budget:

```
0116d495 claude-haiku-4-5 ok=True prompt_chars=4819 cost=0.0395
575a2d8b claude-haiku-4-5 ok=True prompt_chars=4858 cost=0.0346
70a4271a claude-sonnet-5  ok=True prompt_chars=4878 cost=0.0621
```

Re-running the identical cycle: 13.1s wall (entirely the Alpaca data fetch),
**zero** new cache files, **$0.00**, and a byte-identical decision including the
full trader reasoning.

A live (non-dry) cycle then wrote the real trail, and the chain verifies:

```
0 snapshot           {"underlying": "SPY", "spot": 769.35, "realized_vol": 0.0951..., "candidate_count": 12, ...}
1 analyst_view       {"role": "vol_analyst", "probability": 0.32, "abstained": false, ...}
2 analyst_view       {"role": "bear_adversary", "probability": 0.38, "abstained": false, ...}
3 trader_choice      {"choice_id": "ABSTAIN", "aggregate_probability": 0.35, ...}
4 committee_decision {"choice_id": "ABSTAIN", "underlying": "SPY", "structure": null, ...}
5 abstain            {"reason": "committee abstained — trader abstained: ..."}

  Chain:    INTACT
  Tip hash: 479d9be3e598713391bf092f8a2f01646e11e2c46255ae528ad7cce88e1983fc
```

The judge-page replay corpus is no longer empty.

---

## 4. The IV sign convention — live result

**The two analysts now agree on the direction of the vol edge.**

Before (live, earlier the same day): the snapshot said
`IV_MINUS_REALIZED: -0.69pp` and `vol_analyst` called it "a structural edge for
short premium" while `bear_adversary` called it "the market underpriced
volatility". Only the bear was right.

The fix renders the definitional meaning beside the number in
`committee/snapshot.py`:

```
IV_MINUS_REALIZED: -0.69pp (implied is BELOW realized — options are cheap
relative to actual movement, which favours BUYING premium, not selling it)
```

with the mirrored ABOVE/rich/SELLING wording, and a LEVEL branch keyed off the
*rendered* value so a -0.001pp spread printing as "-0.00pp" is not described as
below realized. The same definition is restated verbatim inside both analyst
prompts (`_IV_SIGN_CONVENTION`), so a model that skims the header still cannot
invert it. It states only the definition — never which candidate to pick,
never whether to trade — and `test_the_convention_line_never_prescribes_a_verdict`
holds that line.

After (live, 3 real calls, same SPY snapshot at spot $769.35):

- `vol_analyst` p=0.32 — *"IV is 0.69pp below realized vol, strongly signaling a
  long-volatility regime where implied options are cheap. All 12 candidates are
  bear call spreads (premium-selling), the anti-favored structure type."*
- `bear_adversary` p=0.38 — *"IV is 66 bps below realized vol, so we're selling
  premium that underprices actual movement; bear calls are wrong direction for
  that regime."*

Same sign, same interpretation, both below 0.5, aggregate 0.35 — and the trader
abstained citing the convention explicitly: *"IV is below realized vol, which
favors buying premium, but every candidate this cycle is a bear call credit
spread."* The 0.32 vs 0.38 gap is the bear being *less* pessimistic than the vol
analyst, which is a genuine disagreement about degree, not about direction — the
adversarial role still doing its job.

---

## 5. `python3 scripts/run_session.py --dry-run` with the committee active

Real output, SPY, 2026-08-29, unedited:

```
  SPY spot $769.35 — 632 candidate(s)
  mode: LLM COMMITTEE — vol_analyst + bear_adversary -> trader -> thesis veto + blind veto
  committee:
    vol_analyst      p=0.32 — IV is 0.69pp below realized vol, strongly signaling a long-volatility regime where implied options are cheap. All 12 candidates are bear call spreads (premium-selling), the anti-favored structure type. Theta decay over 10–11 DTE provides modest support, but the signal—vol expansion as realized and implied converge—will likely hurt these shorts.
    bear_adversary   p=0.38 — IV is 66 bps below realized vol, so we're selling premium that underprices actual movement; bear calls are wrong direction for that regime. c1's breakeven (772.33) sits only 0.39% above spot, but 10-day realized vol (9.52%) implies ~1.9% expected move—a gap or upper-tail realization breaches the short 770c easily, capping profit at 233 while max loss is 267, and the long 775c leg (OI=356, width=3.04%) is thin enough that an early exit will bleed slippage.
    aggregate probability: 0.35
    trader: ABSTAIN — IV is below realized vol, which favors buying premium, but every candidate this cycle is a bear call credit spread (pure premium-selling) in the wrong direction for that regime; both analysts flag low conviction (p=0.32, p=0.38) and thin breakeven margins, so no candidate clears the bar.
    veto thesis: VETO — not reached — the cycle abstained before the veto layer
    veto blind:  VETO — not reached — the cycle abstained before the veto layer
  ABSTAIN: committee abstained — trader abstained: IV is below realized vol, which favors buying premium, but every candidate this cycle is a bear call credit spread (pure premium-selling) in the wrong direction for that regime; both analysts flag low conviction (p=0.32, p=0.38) and thin breakeven margins, so no candidate clears the bar.
```

Exit code 0. A refusal is a normal outcome.

Note the veto lines: both report `VETO` with `not reached — the cycle abstained
before the veto layer`. "Not run" is deliberately not a pass, so the flags stay
False. This is the honest rendering of an unreached stage, but it does read as
though a veto fired. Flagging it for review — an explicit `n/a` marker in the
printer (not in the flags) may be clearer for judges.

The same command with `--no-llm` on the same chain reaches a payload:

```
  SPY spot $769.35 — 632 candidate(s)
  mode: DETERMINISTIC (--no-llm) — no LLM in the loop; selecting the most credit per dollar risked
  Selected bear_call_spread: credit $2.83, max loss $217.00
  position greeks: delta -8.0, vega +0.3
  book: 0 position(s), delta +0.0, vega +0.0, day P&L $+0.00, 0 consecutive loss(es), opened today {}
  guard: ALLOW — Within all limits: risk $217.00, 1/3 positions (1 contract(s) approved)
  payload that would be sent:
{
  "order_class": "mleg", "type": "limit", "time_in_force": "day", "qty": "1",
  "limit_price": "-2.83", "client_order_id": "ad0641c4bd02e5179efc373310627849",
  "legs": [
    {"symbol": "SPY261002C00770000", "side": "sell", "ratio_qty": "1", "position_intent": "sell_to_open"},
    {"symbol": "SPY261002C00775000", "side": "buy",  "ratio_qty": "1", "position_intent": "buy_to_open"}
  ]
}
  DRY RUN — no order sent.
```

That contrast is itself the demo: the deterministic selector happily sells
premium into a market where implied is *below* realized. The committee refuses,
and says why.

---

## 6. Changes to existing tests — justification

No test was weakened or deleted. One fixture changed:

**`tests/test_run_session_main.py::bench` now prepends `--no-llm`.**

Every test using `bench` is about preflight, the kill switch, duplicate-order
prevention, data-failure handling, portfolio-state derivation, position counting
or guard short-circuiting. **None** is about candidate selection, so the
selector they run under is incidental to what they assert. Without the flag they
would each spawn four real `claude -p` subprocesses, putting the network in the
suite. The committee path gets its own fixture (`llm_bench`, no `--no-llm`, an
injected committee) and 16 new tests, so coverage went up, not down.

Two assertions inside the *new* committee tests were adjusted while writing
them, before they ever passed: the fake client dispatched on model, but the
analysts and the blind reviewer both run on Haiku, so the roles collapsed; and
the fake chain prices every quote identically, so "highest ratio" and "lowest
ratio" select the same object. Both were bugs in my own scaffolding, not
loosened assertions — the tests now discriminate by role marker and by object
identity respectively.

---

## 7. Open items for review

1. The `veto thesis: VETO — not reached` rendering (section 5) — honest but
   possibly confusing to a judge.
2. `committee/debate.py` (spec §4.1, one bull↔bear round) and
   `committee/premortem.py` are still unbuilt. `decide()` has a natural seam for
   the debate between `run_analysts` and `choose`.
3. `structure_analyst` from the spec's three-analyst diagram is not implemented;
   the README architecture diagram correctly shows only the two that exist.
4. The prompt cache is keyed on the snapshot, which embeds spot to 2dp, so in
   live trading the hit rate within a session is near zero by design. It is a
   replay and audit artifact first, a cost saver second.
