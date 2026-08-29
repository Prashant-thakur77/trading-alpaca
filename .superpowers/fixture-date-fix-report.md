# Judge replay: fixtures drifted with the calendar

Date: 2026-08-30 · Scope: `scripts/replay.py`, `judge/scenarios/{allow,deny,downsize}.json`,
`tests/test_replay.py`

## 1. Root cause

The `/judge` page's entire claim is: *these four recorded decisions replay and
reproduce their recorded verdicts, verifiable by anyone with no credentials.*
That claim held only on the day the fixtures were generated (2026-08-29).

Two defects combined:

1. **Fixtures stored a relative `dte`, never an absolute expiry.**
   `rebuild_intent` therefore anchored `expiry = date.today() + timedelta(days=dte)`.
   The reconstructed expiry advanced one day per calendar day.
2. **`_quote` never set `OptionQuote.as_of`.** `OptionQuote.dte` is
   `(expiry - (as_of or date.today())).days`, so with `as_of` unset the quote was
   measured against the run's clock.

Those two bugs cancelled each other for `dte` — the reconstructed DTE was always
the recorded one — which is exactly why the failure was silent for the gates and
loud only where the absolute date surfaces: the OCC symbol. From 2026-08-30
onward the recomputed payload carried `SPY261001C00777000` where the fixture had
recorded `SPY260930C00777000`, and the payload comparison in `_replay_trade`
reported MISMATCH.

The old `rebuild_intent` docstring argued *for* the relative anchoring ("a fixed
calendar date would make this fixture's liquidity gate and DTE window silently
drift"). The premise was right; the conclusion was wrong. The fix is not "fixed
expiry instead of relative", it is **both dates absolute** — a fixed expiry *and*
the observation date it is measured against. Then `dte` is constant *and* the
symbols are constant.

Observed blast radius on 2026-08-30, before the fix:
`9 failed, 18 passed` in `tests/test_replay.py`; `scripts/replay.py --all --verify`
exited 1 with `allow`, `downsize` MISMATCH. (`deny` still reported OK only because
its recorded outcome is a guard DENY with a `null` payload — there were no OCC
symbols left to compare. It was equally wrong, just unobservable.)

## 2. Schema change (purely additive)

`load_fixture` validates only existence, parseability, dict-ness, a matching
`scenario`, and the presence of `provenance` + `recorded_outcome`. There is no
schema and no unknown-key rejection, so new keys are safe.

| key | level | value | why |
|---|---|---|---|
| `as_of` | top-level | `"2026-08-29"` | the date the option chain was **observed** |
| `expiry` | each candidate | ISO date | the candidate's absolute expiry |

**`as_of` was added explicitly rather than reusing `order_date`.** They coincide
on all three trade fixtures, but they mean different things and must be free to
diverge: `as_of` is when the market was seen (it dates the chain and every
DTE-derived gate); `order_date` is the UTC day the broker-side `client_order_id`
idempotency key belongs to — `options_orders.client_order_id` documents at length
why that one is UTC-and-not-local. A session that observes a chain at 23:50 UTC
and cuts the order at 00:05 UTC has two different correct values. `fixture_as_of()`
falls back to `order_date` when `as_of` is absent, and raises `ScenarioError` when
neither exists — it never falls back to the clock.

`dte` is **kept** in every candidate for readability and backward compatibility,
but it is no longer trusted: `candidate_expiry()` derives DTE from
`(expiry - as_of).days` and raises `ScenarioError` if the fixture's own `dte`
contradicts it. Two representations of one fact that disagree mean a corrupt
judged artifact, so it fails loudly rather than picking a winner.

`fail_closed.json` was deliberately **not** given an `as_of`: it has no candidates,
no chain and no date anywhere in its recorded outcome — its replay path is
date-free. Inventing a date for it would have put an untraceable value in a
judged artifact.

## 3. Code changes — `scripts/replay.py`

- **new `fixture_as_of(name, fixture) -> date`** — reads `as_of`, falls back to
  `order_date`, raises rather than consulting the clock.
- **new `candidate_expiry(candidate, as_of) -> date`** — reads the absolute
  expiry from the candidate and/or its legs, rejects conflicting expiries,
  rejects a candidate with no expiry at all, and cross-checks `dte`.
- **`_quote(leg, underlying, expiry, as_of)`** now passes `as_of=as_of` into
  `OptionQuote`, following the precedent already set by
  `seed_replay.py:quote_from_bar`.
- **`rebuild_intent(candidate, underlying, contracts, as_of)`** takes the
  observation date and uses `candidate_expiry`; `date.today()` is gone from the
  module (the now-unused `timedelta` import went with it).
- **`client_order_id` is now always passed an explicit `on=`** (`order_date`, else
  `as_of`). Previously a fixture without `order_date` would have silently fallen
  back to "the UTC date of this run", i.e. the same class of bug one field over.
- The `snapshot` trace stage now reports `as_of`, so the observation date is
  visible in `--json` output rather than implicit.

## 4. TDD evidence

Tests were written first and observed red before any implementation change:

```
$ python3 -m pytest tests/test_replay.py -q     # tests added, replay.py untouched
46 failed, 29 passed
```

Two new classes, 176 added lines, **zero deleted lines** in `tests/test_replay.py`:

- `TestClockIndependence` — freezes `date.today()` (a `date` subclass injected
  into `candidate_builder` and `replay`; the real system clock is never touched)
  at `as_of`, `as_of + 3 days` and `as_of + 30 days`, and asserts, for all four
  scenarios at all three dates: `replay_scenario(...).matched`; `verify_all()`
  all-green; `render_json()` **byte-identical** to the unfrozen baseline (not
  merely "still matches"); recomputed payload OCC symbols equal to the recorded
  ones; reconstructed `intent.dte` equal to the recorded `dte`.
- `TestFixtureDateSchema` — each trade fixture declares `as_of == 2026-08-29`;
  every candidate's `expiry - as_of == dte`; the chosen candidate's expiry stamp
  equals the `%y%m%d` field of the recorded OCC symbols; a candidate whose `dte`
  contradicts its `expiry` is rejected; a candidate with **no** expiry is rejected
  rather than guessed (the specific regression guard against reintroducing
  `today + dte`); a trade fixture with neither `as_of` nor `order_date` is
  rejected; every rebuilt leg quote carries `as_of`.

```
$ python3 -m pytest tests/test_replay.py -q     # after the fix
75 passed
$ python3 -m pytest tests/ -q
849 passed in 63.69s
```

### The nine previously-failing tests

All nine were read before touching anything. **None encodes the buggy behaviour**,
and none was modified — `git diff --numstat tests/test_replay.py` reports
`176 0`: additions only, no deletion, no weakening.

They are: `TestReplayEachScenario::test_replays_and_matches_recorded_outcome[allow]`
and `[downsize]`; `TestVerifyAll::test_verify_all_scenarios_pass`;
`TestNoNetworkNoLLM::test_replay_never_calls_the_llm_client` and
`::test_replay_works_with_no_alpaca_env_vars_set`; and four `TestCLI` cases
(`--scenario allow`, `--all`, `--all --json`, `--verify` exit 0). Every one of
them asserts the *correct* property — "the replay reproduces the recorded
outcome" / "the process exits 0" — and each was a true report of a real defect.
They pass now because the defect is gone, not because the assertion moved.

The buggy behaviour was encoded in *prose*, not in a test: the old
`rebuild_intent` docstring asserted that anchoring to `date.today()` was what
preserved the reproducibility guarantee. That docstring was rewritten to record
the actual failure mode.

## 5. Three-date verification

Frozen-clock subprocess runs of the real CLI, credential-free
(`env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY`), with `date.today()` overridden in
`candidate_builder`, `exit_monitor`, `replay` and `run_session`:

```
$ CLOCK=2026-08-29 python3 scripts/replay.py --all --verify   # as_of
allow        OK
deny         OK
downsize     OK
fail_closed  OK
exit=0

$ CLOCK=2026-09-01 python3 scripts/replay.py --all --verify   # as_of + 3
allow        OK
deny         OK
downsize     OK
fail_closed  OK
exit=0

$ CLOCK=2026-09-28 python3 scripts/replay.py --all --verify   # as_of + 30
allow        OK
deny         OK
downsize     OK
fail_closed  OK
exit=0
```

Also green at `2026-12-31` and `2027-06-15` — well past every fixture's expiry,
which is the real proof that nothing in the replay path consults the clock any
more.

Real clock (2026-08-30), both required forms:

```
$ python3 scripts/replay.py --all --verify                                  # exit 0
$ env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY python3 scripts/replay.py --all --verify   # exit 0
$ make judge                                                                # all four MATCHED
```

## 6. No recorded output changed

Mechanically verified: strip the two new keys (`as_of` top-level, `expiry` per
candidate) from each new fixture and compare the parsed structure to
`git show HEAD:<path>`:

```
allow        identical-after-stripping-new-keys=True  added_top_level=['as_of']
deny         identical-after-stripping-new-keys=True  added_top_level=['as_of']
downsize     identical-after-stripping-new-keys=True  added_top_level=['as_of']
fail_closed  identical-after-stripping-new-keys=True  added_top_level=[]
```

`git diff --numstat` on the three fixtures: `13 0`, `13 0`, `13 0` — additions
only. No `provenance`, analyst view, trader reasoning, veto result, guard
decision/reason/approved_contracts, `recorded_outcome`, payload or stdout string
was touched. `fail_closed.json` is byte-identical to HEAD.

Every added value is traceable, not invented:
`as_of` is the `order_date` already committed in each fixture, and each
`expiry = as_of + dte` using the `dte` already committed. The result was
cross-checked against the independent record of the same fact — the `%y%m%d`
field of the OCC symbols in `recorded_outcome.chosen_intent.legs` and
`recorded_outcome.payload.legs` (`SPY260930…` for `allow`/`downsize`,
`SPY260908…` for `deny`). Both derivations agree; that check is now a permanent
test.

## 7. Note / follow-up (out of scope, not done here)

`judge/index.html` is generated by `scripts/build_judge_page.py` and is already
stale with respect to a *previously committed* `fx.py` restyle. Regenerating it
pulls in ~200 lines of unrelated CSS from that earlier commit and changes nothing
this fix affects — the page renders `id / structure / dte / credit` and the
recorded payload, none of which moved. It was deliberately left untouched to keep
this commit surgical. `make judge-page` should be re-run as part of the site work,
not here.
