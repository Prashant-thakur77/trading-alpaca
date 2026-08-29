# Is there a measurable vol smile deviation in our chain? Measured 2026-08-30

Run before designing the richness feature, so the design could be shaped by the
answer rather than assuming one. Live SPY chain, spot 769.35, 3,686 contracts.

## Can we fit at all

Applying our own risk.yaml gates first (OI >= 100, quote width <= 10% of mid),
then solving IV per strike and dropping non-convergent or absurd solves:

    14 of 17 expiry/right groups retain >= 8 liquid strikes.

So a per-expiry fit is supportable on real data. Note the raw chain contains
deep-ITM calls solving to 240% IV; those are rejected, not fitted.

## Is any deviation bigger than the quote noise

Noise floor per strike is taken as half the IV interval implied by its own
bid-ask, i.e. (IV(ask) - IV(bid)) / 2. Signal is the RMSE of the residual from
a polynomial fit of IV against standardised moneyness m = ln(K/S)/sqrt(T).

| moneyness band | degree | groups | mean RMSE | mean noise | signal/noise |
|---|---|---|---|---|---|
| 1.0 | 2 | 14 | 0.383v | 0.070v | **5.51x** |
| 1.0 | 3 | 14 | 0.265v | 0.070v | 3.81x |
| 0.5 | 2 | 14 | 0.197v | 0.064v | 3.08x |
| 0.5 | 3 | 14 | 0.143v | 0.064v | 2.24x |
| 0.3 | 2 | 14 | 0.174v | 0.067v | 2.58x |
| 0.3 | 3 | 14 | 0.124v | 0.067v | 1.85x |
| 0.2 | 2 | 14 | 0.141v | 0.069v | 2.05x |
| 0.2 | 3 | 14 | 0.112v | 0.069v | **1.63x** |

## What this means

The headline 5.51x is not evidence of mispricing. Signal-to-noise falls
monotonically as the moneyness band narrows and the polynomial degree rises,
which is the signature of **model misspecification**: a quadratic cannot
represent a real skewed smile across a wide moneyness range, so its residuals
are large for reasons that have nothing to do with the market. Roughly 70% of
the apparent signal disappears under a properly specified local fit.

What survives at the best-specified configuration is 1.63x. Real, but modest.

## Economic significance, which is the part that matters

At band 0.2 / degree 3 a typical residual is 0.11 vol points. Vega on a 30-DTE
ATM SPY option is roughly 0.7 per contract per vol point, so 0.11 vol points is
worth on the order of **$8 per contract** against a bid-ask spread of several
dollars.

**The measured richness is smaller than the cost of crossing the spread.**

That rules out richness as an alpha source and supports exactly one use: a
**tie-breaker among strikes that are already viable** on liquidity, DTE and
guard grounds. It must never be described as edge, and any deviation below the
half-spread threshold must score as zero.

This measurement is also the acceptance criterion for shipping the feature on
by default: if walk-forward with richness scoring is not clearly better than
without, it ships behind a flag defaulting to off, and this file is cited in
"what we cannot prove".
