# Volatility smile fit + realized-vol rank — implementation report

Additive change to `analytics.py` and `tests/test_analytics.py`. No existing
signature or behaviour changed; no existing test weakened or deleted.

## What shipped

`analytics.py`:

- `SmileFit` (frozen) + `NO_SMILE` module-level absent instance, mirroring
  `OptionGreeks` / `ZERO_GREEKS`. Fields: `expiry, right, coeffs, degree,
  n_points, rmse, band, t_years`; methods `curve_at`, `residual_for`,
  property `is_measured`. `t_years` is stored, not recomputed, because a fit is
  only meaningful against the T it was built with.
- `fit_smile(quotes, spot, *, band=0.2, degree=3, min_points=8) -> SmileFit|None`
  — one (expiry, right) group, IV vs `m = ln(K/S)/sqrt(T)`. Mixed input returns
  `None` rather than silently fitting a subset.
- `fit_smiles(quotes, spot, ...) -> dict[(expiry, right), SmileFit]` for a whole
  chain, deterministic iteration order.
- `richness(quote, fit, spot) -> float` — signed vol points, exactly `0.0` when
  `|residual| < (IV(ask) - IV(bid)) / 2`.
- `realized_vol_rank(bars, window=252, *, vol_window=20) -> float|None`.
- `group_by_expiry(quotes)` extracted from `atm_implied_vol` so the smile code
  reuses the existing grouping rather than adding a second one. `atm_implied_vol`
  behaviour is unchanged (its tests still pass untouched).
- Module docstring now records the misspecification finding and the two IMC
  teams who abandoned a vol surface, so the design rationale survives.

Nothing raises. Every failure path returns `None` (unmeasurable) or `0.0`
(no signal), per the module's fail-closed contract.

## TDD evidence

18 new tests were written and run **before** any implementation existed:

    18 failed, 29 passed   (all 18 failing on ImportError — the red bar)

After implementing: `47 passed` in `tests/test_analytics.py`.
Full suite: **849 passed, 0 failed** (`python3 -m pytest tests/ -q`, 63s). The 9
`tests/test_replay.py` failures noted in the brief were already fixed by the
concurrent process; no new failures were added.

### Mutation checks (the tests actually discriminate)

- **IV cap.** Raising `MAX_FITTABLE_IV` from 1.5 to 999 admits the planted
  240%-IV contract: `n_points` 21 → 22, coefficients change, and RMSE goes from
  `0.000000` to `0.4454`. One bad point really does destroy the fit, so
  `test_absurd_iv_outlier_is_excluded_and_does_not_move_the_fit` is load-bearing.
- **Half-spread gate.** On the gated case the noise floor is `0.00686` and the
  raw residual `0.00340` → `richness == 0.0` exactly. The same chain with a 10x
  larger planted bump scores `0.04252`, so the gate is not simply returning zero
  for everything.

### Planted-deviation recovery

Synthetic 21-strike chain, S=450, 30 DTE, known vol function
`iv(m) = 0.20 - 0.06m + 0.15m²`, with **+0.0500** planted at K=460.

    richness recovered: 0.04252  (85.0% of the planted size)
    untouched strike K=445:      -0.0015

The 15% shortfall is not error — the bumped strike is itself in the fit, so
least squares pulls the curve toward it and the residual is `(1 - leverage) x`
the true deviation. The test states this and asserts 60–105% of the planted
size rather than an arbitrary tolerance. The bias is conservative, which is the
right direction for a number this small.

## Live SPY chain

Real fetch, 2026-08-30, spot **769.35**, 3,686 contracts, 1,078 surviving
`candidate_builder.passes_liquidity` (OI ≥ 100, width ≤ 10% of mid, DTE 7–45).

    expiry/right groups present:   17
    groups that produced a fit:    14   ← matches docs/research/smile-feasibility.md exactly

| expiry | right | n_points | rmse |
|---|---|---|---|
| 2026-09-08 | c | 19 | 0.0006 |
| 2026-09-08 | p | 17 | 0.0008 |
| 2026-09-09 | c | 16 | 0.0008 |
| 2026-09-09 | p | 11 | 0.0005 |
| 2026-09-11 | c | 50 | 0.0032 |
| 2026-09-11 | p | 41 | 0.0015 |
| 2026-09-18 | c | 50 | 0.0043 |
| 2026-09-18 | p | 45 | 0.0009 |
| 2026-09-25 | c | 45 | 0.0010 |
| 2026-09-25 | p | 47 | 0.0009 |
| 2026-09-30 | c | 68 | 0.0041 |
| 2026-09-30 | p | 55 | 0.0010 |
| 2026-10-02 | c | 39 | 0.0008 |
| 2026-10-02 | p | 39 | 0.0011 |

The 3 groups that produced no fit are the ones with fewer than 8 liquid strikes
inside |m| ≤ 0.2 — they are absent from the result dict, so a caller cannot
mistake a missing smile for a flat one.

### Distribution of non-zero richness — say it plainly

    strikes scored:  1,070
    non-zero:          230   →  21.5%
    ZERO after the half-spread rule:  78.5%

Nearly four strikes in five score exactly 0.0. That is the gate working, not a
bug: on a liquid SPY chain most strikes sit inside their own quote noise.

Of the 230 that do score (absolute vol units; x100 = vol points):

    min 0.0002   p25 0.0005   median 0.0008   p75 0.0012   max 0.0037   mean 0.0009

In money, using each strike's own Black-Scholes vega:

    median |richness|:  $3.94 per contract
    max    |richness|: $30.55 per contract
    median quoted spread: $4.00 per contract

**The median scoring strike is worth $3.94 against a $4.00 cost to cross.** The
brief's central claim is confirmed on live data by an independent measurement:
this is not tradeable as edge. It is a tie-breaker among already-viable strikes,
and the docstrings say so in those words.

### One discrepancy worth flagging

Measuring signal-to-noise the way `docs/research/smile-feasibility.md` does, on
the same chain at the same configuration (band 0.2, degree 3):

    mean fit RMSE:    0.00155  (0.155 vol points)
    mean noise floor: 0.00365  (0.365 vol points)
    signal / noise:   0.42x        ← the research doc tabulates 1.63x

The RMSE terms agree to within a factor of ~1.4 once the doc's `0.112v` is read
as percent units (0.00112 absolute). The **noise** term does not: mine is ~5x
larger. So the gap is in how the per-strike noise floor was computed, not in the
fit. I did not modify that document — but whichever noise figure is right, the
conclusion moves in the same direction: at 0.42x the mean residual is *below*
the mean quote noise, which is a stronger version of the doc's finding, not a
weaker one. If anyone later wants to revive richness as a signal, this number is
the first thing to re-derive.

## Realized-vol rank

`realized_vol_rank(bars, window=252, *, vol_window=20)`. Each history point is
`realized_volatility` over `vol_window` bars, so it reuses the desk's own
estimator. Ties are mid-ranked.

Measured on deterministic synthetic paths (alternating ±sigma, no RNG):

    calm history then a shock:   0.983
    volatile history then calm:  0.015
    today between two regimes:   0.431
    perfectly flat series:       0.500
    101 bars, window 252:        None

`None` on insufficient history is deliberate and tested by name: a fabricated
0.5 would look like a measurement and silently not be one.

The flat-series answer of 0.5 is the correct mid-rank of a fully tied sample —
today is exactly as typical as every other day — and the docstring directs
callers who must distinguish "typical" from "no variation at all" to read
`realized_volatility`, which returns 0.0 there.

**Named honestly.** This is a *realized*-vol rank. True IV-rank needs a history
of ATM implied vol we do not have: the live journal holds 2 snapshots with
`atm_iv = None` and the 23 seed snapshots carry none either. The docstring
records that limitation so nobody later relabels it.

## Not done / out of scope

- Not wired into `committee/`, `scripts/run_session.py`, or the ranking logic.
  Purely additive to `analytics.py`; adoption as a tie-breaker is a separate
  change, and per smile-feasibility.md it should ship behind a flag defaulting
  to off unless walk-forward shows an improvement.
- `scripts/replay.py` and `judge/scenarios/*.json` untouched (concurrent work).
