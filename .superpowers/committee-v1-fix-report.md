# committee v1 — fix report

Baseline at start: **389 passing**. All work TDD (failing test first, watched
it fail for the stated reason, then fixed). No test was weakened or deleted;
the two test changes are justified inline below.

---

## CRITICAL 1 — a plausible model reply crashed the whole cycle

**Was:** `_extract_json` returned whatever `json.loads` produced. Typed
`dict | None`, never checked. Valid-JSON-that-is-not-an-object flowed through
with `ok=True` and then exploded at `trader.py:78` / `analysts.py:105`
(`AttributeError`) and `veto.py:132` (`TypeError`). The trader prompt itself
offers "the literal string ABSTAIN", so a bare `"ABSTAIN"` reply — the most
natural abstention the model can write — crashed the cycle instead of
abstaining.

**TDD evidence:** added 8 client-level tests plus one per call site, then ran
them against the unfixed code:

```
15 failed, 10 passed
llm/client.py:87  TypeError: expected string or bytes-like object
llm/client.py:142 AttributeError: 'Junk' object has no attribute 'returncode'
committee/analysts.py:105 AttributeError: 'str' object has no attribute 'get'
committee/trader.py:78    AttributeError: 'str' object has no attribute 'get'
committee/veto.py:132     TypeError: argument of type 'float' is not iterable
```

**Now:** `_loads_dict` parses and returns a value *only* if it is a dict;
each of the three tiers uses it and falls through otherwise. If no tier
yields a dict, `parsed=None`, `ok=False`, raw text preserved. Note the
deliberate consequence, asserted in a test: `[{"choice": "c1"}]` falls
through tiers 1-2 and is then recovered by the balanced-brace tier as
`{"choice": "c1"}` — a dict, so no caller can explode on it.

Defence in depth at all three call sites: `analysts._view_from_response`,
`trader.choose` and `veto.blind_review` each check
`isinstance(response.parsed, dict)` and abstain/veto otherwise. The veto
check matters twice over: `"agree" not in "ABSTAIN"` is a *substring* test
that quietly returned "no veto" rather than raising.

**Covering tests:** `tests/test_llm_client.py::test_bare_json_string_reply_is_not_a_dict_and_abstains`,
`…_json_float_…`, `…_json_bool_…`, `…_json_list_of_scalars_…`,
`…_fenced_json_list_of_scalars_abstains`,
`…_top_level_list_wrapping_one_object_falls_through_to_brace_tier`,
`…_fenced_list_wrapping_one_object_…`,
`…_parsed_is_always_dict_or_none_across_every_measured_reply`;
`tests/test_trader.py::test_non_dict_parsed_payloads_abstain_rather_than_raise`;
`tests/test_analysts.py::test_non_dict_parsed_payloads_abstain_rather_than_raise`
(both analysts × 5 payloads);
`tests/test_veto.py::test_blind_review_non_dict_parsed_payloads_veto_rather_than_raise`.

## CRITICAL 2 — `call_claude` raised, contradicting its own docstring

**Was:** `proc.returncode` sat outside any try; `envelope.get` assumed a dict;
`float(total_cost_usd)` assumed a number; `result` was fed to `re.search`
without being a string.

**TDD evidence:** the same failing run above — `AttributeError` on a junk
runner return at `client.py:142`, plus failures for envelopes `[] / 5 /
"hello" / null`, `total_cost_usd: "abc"` and a dict `result`.

**Now:** the whole body is wrapped (`call_claude` delegates to
`_call_claude_inner`; any escaping exception becomes
`ok=False, error="claude CLI wrapper error: …"`). Inside: `returncode` and
`stdout` read via `getattr`, the envelope must be a dict, `result` must be a
str, and cost goes through `_float_or_zero` (non-numeric or non-finite → 0.0,
because a bad telemetry field is not a bad answer — that call still returns
`ok=True` with its parsed dict).

**Covering tests:** `tests/test_llm_client.py::test_non_dict_envelope_{list,number,string,null}_returns_ok_false_never_raises`,
`test_non_numeric_cost_is_coerced_not_raised`,
`test_non_string_result_field_returns_ok_false_never_raises`,
`test_runner_returning_a_junk_object_returns_ok_false_never_raises`.

This is what makes the README's "a failed LLM call abstains" true rather than
aspirational.

## IMPORTANT 3 — the id→candidate mapping is now returned

`render_snapshot` now returns a frozen `Snapshot(text, candidates)` with a
`candidate_ids` property. One piece of code owns the sort, the cap and the id
assignment, so no orchestrator can re-derive them differently and send a
different trade than the id names while every guard reports PASS.

**Covering tests:** `tests/test_snapshot.py::test_render_returns_text_and_id_to_intent_mapping`,
`test_id_rendered_in_text_names_the_intent_the_mapping_returns` (the id in
the text and the id in the mapping name the same structure, DTE and legs),
`test_shuffling_the_input_does_not_change_which_intent_an_id_refers_to`
(3 shuffles, identical text and identical mapping),
`test_mapping_only_contains_the_capped_candidates`.

**Justified test change:** the existing snapshot tests now assert on
`.text` instead of on a bare string. The return contract is exactly what the
finding required changing; no assertion was removed or loosened.

## IMPORTANT 4 — the snapshot now carries the analysts' decision variables

Each leg renders `iv=… oi=… bid=… ask=… width=…`; each candidate renders
`mean_leg_iv`; the header renders `IMPLIED_VOL_ATM` and `IV_MINUS_REALIZED`
(ATM implied minus realized, in percentage points — the decision variable for
premium selling). ATM IV is the closest-to-the-money leg with a solvable IV,
ties broken on `(|strike-spot|, strike, right)` so the choice is
deterministic. An unsolvable IV renders as `iv=unavailable` (and
`IMPLIED_VOL_ATM: unavailable`), never omitted, so "no data" cannot be read
as "low vol". All values fixed-precision, so the snapshot stays
byte-deterministic and prompt-hash caching still works.

**Covering tests:** `tests/test_snapshot.py::test_each_leg_renders_implied_vol_open_interest_and_quote_width`,
`test_header_reports_implied_vol_and_the_iv_minus_realized_spread`,
`test_unsolvable_iv_is_rendered_as_explicitly_unavailable_not_omitted`,
`test_implied_vol_rendering_stays_deterministic`.

### Live acceptance result

**Yes — the vol analyst now produces a probability instead of abstaining.**

Run against a real snapshot: live Alpaca SPY data (spot 769.35, 30 daily
bars → realized vol 9.52%, a 3,686-quote option chain → 617 candidates built,
12 rendered), with real `claude -p` calls (4 in the acceptance run: the pair
sequentially, then the pair concurrently).

| run | vol_analyst | bear_adversary |
|---|---|---|
| sequential | `abstained=False`, **p=0.47** | `abstained=False`, p=0.36 |
| concurrent | `abstained=False`, **p=0.30** | `abstained=False`, p=0.42 |

Before the fix the reviewer measured the vol analyst abstaining with
"Implied volatility data is not provided" on every call. It now reasons
directly from the fields that were added, quoting them back:

> "Realized vol (9.52%) trades +69bp rich to implied (8.83%), a headwind for
> premium sellers. While theta decay is powerful over 10-11d and short-leg
> liquidity (esp. c1 with 3968 OI) is solid…"

The realized-vs-implied spread, the DTE and the open interest in that
sentence are all fields this fix put into the snapshot. A later confirmation
run (see latency below) gave `p=0.70`. Three of three vol-analyst calls
against the new snapshot returned a probability; zero abstained.

## IMPORTANT 5 — the thesis check no longer flips on contract count

`thesis_check`'s neutral band is now `NEUTRAL_DELTA_THRESHOLD *
max(intent.contracts, 1)`, because `position_greeks` scales by contracts and
this test asks a question about the *structure*, not the size — sizing is
RiskGuard's job, and it can downsize rather than refuse. The value 15 is
kept and documented as a heuristic (half risk.yaml's 30-delta cap).

**TDD evidence:** before the fix the new test reported exactly the behaviour
the review measured:

```
AssertionError: size changed the verdict: [True, False, False]
```

**Covering tests:** `tests/test_veto.py::test_neutral_thesis_verdict_is_invariant_to_contract_count`
(same straddle at 1, 2, 3 contracts → same verdict, and it is True) and
`test_neutral_band_still_rejects_a_directional_position_at_any_size` (a
+193-delta position mislabelled `iron_condor` still fails at every size, so
size-invariance did not become "anything passes").

## MINORS

- **Tautological veto test** — replaced by
  `test_blind_review_prompt_contains_exactly_the_intent_derived_fields`,
  which asserts positively: the signature is exactly
  `{intent, spot, realized_vol, client}` (so the test fails the moment a
  views/debate parameter is threaded in — the property the old test was
  gesturing at) and the prompt contains each intent-derived field.
  *Justified test change: the old assertions could not fail.*
- **`_candidate_sort_key` omitted `contracts`** — added, with a test
  (`test_ties_on_every_other_field_are_broken_by_contract_count`) that renders
  two same-shape different-size candidates in both input orders and requires
  identical text.
- **`PromptCache.put` was a non-atomic `write_text`** — now `mkstemp` +
  `fsync` + `os.replace` (atomic within the directory), and `get` treats an
  unreadable or non-object record as a **miss** rather than raising forever
  on the /judge replay path. Tests:
  `test_truncated_record_reads_as_a_miss_not_an_exception`,
  `test_record_that_is_not_an_object_reads_as_a_miss`,
  `test_put_leaves_no_temporary_file_behind`,
  `test_a_failed_put_leaves_the_previous_record_intact`.
- **Uncapped analyst prose in the trader prompt** — `MAX_VIEW_REASONING_CHARS
  = 600` per view, applied to both `reasoning` and `abstain_reason`, with a
  `[...truncated]` marker. Tests:
  `test_analyst_prose_is_capped_before_it_reaches_the_trader_prompt`,
  `test_short_analyst_prose_is_passed_through_unchanged`.
- **Abstaining views carried `probability=0.0`** — now `None`, with the
  invariant documented loudly at the field ("abstained is True iff
  probability is None"). Tests:
  `test_abstained_view_carries_none_probability_not_zero` (5 distinct abstain
  paths) and `test_active_view_probability_is_never_none` (a genuine 0.0 view
  survives as a number). `aggregate` already excluded abstainers, so no
  arithmetic touches the None.

## PERFORMANCE — analysts now run concurrently

`committee.analysts.run_analysts(snapshot, client=None)` runs the roles on a
`ThreadPoolExecutor` (the work is entirely waiting on a `claude -p`
subprocess), returns views in a stable `ANALYSTS` order regardless of which
finished first, and converts a raising worker into an abstention rather than
letting it escape.

**Measured (real `claude -p` calls, full 12-candidate snapshot prompt):**

| measurement | sequential | concurrent |
|---|---|---|
| acceptance run, analyst pair, wall clock | 77.6s | 84.4s |
| confirmation run, analyst pair | 56.8s (sum of the two calls) | **28.9s** wall |

The acceptance run's numbers are noise-dominated — per-call latency on this
prompt varies from ~28s to ~45s, which is larger than the effect being
measured, so that one pairing came out slower. Two further measurements
settle it:

1. **Do two `claude` CLI invocations overlap at all?** Two concurrent trivial
   calls took 6.3s against 6.1s for a single one — **1.03x**, so they overlap
   almost perfectly; nothing serializes them.
2. **Instrumented pair on the real snapshot:** individual calls 28.9s and
   27.8s, concurrent wall clock **28.9s** = max(individual), versus 56.8s if
   run in sequence — a **1.96x** speedup, which is the theoretical maximum
   for two calls.

So the pair now costs `max(latency)` rather than `sum(latency)`. (Total real
calls made across all of this: 9 — 4 for the acceptance run, 3 for the
overlap probe, 2 for the instrumented pair. All outside pytest; the test
suite makes no network calls.)

Unit test for the property without touching the network:
`test_run_analysts_overlaps_the_two_calls` (two 0.4s fake calls must complete
in < 0.7s), plus `test_run_analysts_returns_both_views_in_a_stable_order` and
`test_run_analysts_turns_a_raising_client_into_an_abstention`.

## Not fixed / notes

- `run_analysts` is the concurrent entry point, but nothing in
  `scripts/run_session.py` calls the committee yet (the session still picks
  the best credit-per-risk candidate deterministically — Plan 3 work). The
  `Snapshot` mapping and `run_analysts` are therefore ready for that
  orchestrator but have no production caller to update today. This is why
  Finding 3's harm was still latent: there is no orchestrator yet that could
  have re-derived the ids wrongly.
- `float(True)` is still accepted as a probability of 1.0 in
  `_view_from_response` (a JSON `true` in the `probability` field). Out of
  scope for these findings and not observed; noted for the next pass.

## Test count

389 at baseline → **429** passing. No network in the
suite; the only real `claude -p` calls were the 4 in the Finding 4
acceptance run, executed outside pytest.
