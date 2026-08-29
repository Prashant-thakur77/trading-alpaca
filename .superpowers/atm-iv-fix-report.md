# ATM IV correctness fix — report

## The defect

`committee/snapshot.py:256` computed `IMPLIED_VOL_ATM` (and thus
`IV_MINUS_REALIZED`) from the CAPPED, surfaced candidate list rather than
from the option chain / market. That made the analysts' primary vol-regime
signal a function of which candidates a caller happened to build and which
survived the `max_candidates` stratified cap — not of the market. On one
unchanged SPY chain (same spot, same bars, same session) surfacing only bear
call spreads (all OTM calls, low on the vol skew) produced
`IMPLIED_VOL_ATM 8.83%` / `IV_MINUS_REALIZED -0.69pp`; adding straddles
flipped it to `+0.72pp` — the committee's entire premium-buy-vs-sell verdict
reversed although the market never moved.

## TDD evidence

Failing tests were written and run BEFORE any implementation change, and
confirmed to fail for the intended reason (missing symbol / missing kwarg,
not a typo or import error):

```
tests/test_analytics.py -k AtmImpliedVol
  ImportError: cannot import name 'atm_implied_vol' from 'analytics'

tests/test_snapshot.py (6 new tests)
  TypeError: render_snapshot() got an unexpected keyword argument 'atm_iv'
  (plus one caplog assertion failing because no warning had been logged yet)

tests/test_decide.py::TestHappyPath::test_decide_forwards_atm_iv_to_the_rendered_snapshot
  TypeError: render_snapshot() got an unexpected keyword argument 'atm_iv'
```

After implementing the fix, the same tests pass, and the full suite is
green:

```
python3 -m pytest tests/ -q
509 passed
```

(497 original + 12 new: 5 in test_analytics.py, 6 in test_snapshot.py, 1 in
test_decide.py. No existing test was weakened, skipped, or deleted.)

## The fix

1. **`analytics.py`** — new `atm_implied_vol(chain, spot, dte_target=30) ->
   float | None`. Groups the chain by expiry, picks the expiry nearest
   `dte_target`, picks the strike nearest `spot` within that expiry, solves
   IV for the call and the put at that strike via `analytics.implied_vol`,
   and returns their mean. Ties are broken deterministically (by expiry
   date, then by strike). Returns `None` for an empty chain or when neither
   leg's IV solves — never a guess.

2. **`committee/snapshot.py`** — `render_snapshot` gained an `atm_iv`
   parameter. A private sentinel (`ATM_IV_NOT_SUPPLIED`, not literal `None`)
   distinguishes three states cleanly:
   - **caller supplies a float** → rendered verbatim, independent of
     `candidates`. This is the fix.
   - **caller explicitly supplies `None`** → the market computation ran and
     could not establish a value → renders `IMPLIED_VOL_ATM: unavailable` /
     `IV_MINUS_REALIZED: unavailable`. Falling back to the candidate-derived
     estimate here would silently resurrect the exact bug being fixed, so it
     does not.
   - **caller omits the argument entirely** → backward-compatible fallback
     to the old candidate-derived estimate (renamed
     `_candidate_derived_atm_iv`, docstring updated to say "FALLBACK ONLY,
     selection-dependent"), with a `logger.warning` that the value is
     selection-dependent.

3. **`committee/decide.py`** — `decide()` / `_decide_inner()` gained the same
   `atm_iv` parameter (defaulting to the same sentinel) and pass it straight
   through to `render_snapshot`, unmodified.

4. **`scripts/run_session.py`** — after fetching the option chain and before
   building candidates' committee input, computes
   `market_atm_iv = atm_implied_vol(chain, spot)` once from the full chain
   and prints it. `_default_committee` was refactored into a
   `_make_default_committee(atm_iv)` factory that closes over this value and
   forwards it to `committee_decide(..., atm_iv=atm_iv)`. This was necessary
   because `run_committee` calls every committee — including injected test
   doubles in `tests/test_run_session_main.py` — as a fixed 5-argument
   `committee(underlying, spot, realized_vol, candidates, journal)`; adding
   `atm_iv` as a 6th argument there would have broken every `FakeCommittee`
   test double. Binding it via closure instead left that shared call surface
   untouched.

## Live verification (2026-08-29, SPY, paper data)

`python3 scripts/run_session.py --dry-run --no-llm`:
```
SPY spot $769.35 — 632 candidate(s)
ATM IV (chain-derived, ~30 DTE): 11.78%
```

`python3 scripts/run_session.py --dry-run` (full LLM committee):
```
SPY spot $769.35 — 632 candidate(s)
ATM IV (chain-derived, ~30 DTE): 11.78%
mode: LLM COMMITTEE — vol_analyst + bear_adversary -> trader -> thesis veto + blind veto
committee:
  vol_analyst      p=0.80 — IV is +2.27pp above realized vol—options are rich,
                    strongly favouring premium-selling structures...
  bear_adversary   p=0.43 — The +2.27% IV advantage is razor-thin—realized vol
                    need only rise from 9.52% to ~10.7%... breakeven sits at
                    772.33, just $3 above current spot (0.39% buffer)...
  aggregate probability: 0.61
  trader: ABSTAIN — the bull/bear split (0.80 vs 0.43) reflects real
          disagreement... no candidate offers enough margin of safety
ABSTAIN: committee abstained — trader abstained (as above)
```

Both analysts now read the SAME chain-derived `IV_MINUS_REALIZED: +2.27pp`
header and draw *consistent* conclusions about the sign (both call it
"IV rich / above realized" — no repeat of the earlier -0.69pp-read-both-ways
incident). The trader still ABSTAINED, but for a substantive reason (thin
breakeven margin, real analyst disagreement) — not because the header
number was an artifact of which candidates happened to be built.

**Direct confirmation of `analytics.atm_implied_vol` on the live chain**
(computed independently from the same bars/chain `run_session.py` used):

```
spot           = 769.35
realized_vol   = 9.5165%
atm_iv (chain) = 11.7828%
iv - realized  = +2.2663 pp
```

**The market's true regime**: implied volatility (11.78%) is ABOVE realized
volatility (9.52%) by +2.27pp — options are priced rich relative to how much
SPY has actually been moving, which favours premium-SELLING structures
(credit spreads), not buying. This is the number the analysts should have
been reading all along, and — unlike the pre-fix number — it does not change
if a different mix of candidates happens to be surfaced.

## Constraints honored

- No existing test weakened, skipped, or deleted.
- Snapshot determinism preserved: same inputs (including `atm_iv`) still
  produce byte-identical text; shuffling `candidates` with `atm_iv` supplied
  changes nothing (`test_render_is_deterministic_with_atm_iv_supplied`,
  `test_shuffling_candidates_does_not_change_the_atm_iv_line_when_supplied`).
- `python3 -m pytest tests/ -q` → 509 passed, run before this report was
  written.
