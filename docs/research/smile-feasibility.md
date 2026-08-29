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

## CORRECTION, 2026-08-30: the aggregate ratio is not robust

The 1.63x above is **one of several defensible aggregations, and should not be
quoted as a headline number**. Re-measured on the same chain, the same fit and
the same band, varying only how the per-strike noise is aggregated:

| aggregation of the noise term | noise | signal/noise |
|---|---|---|
| mean of per-group medians (the table above) | 0.069v | 1.63x |
| pooled median across all strikes | 0.053v | 2.12x |
| pooled mean across all strikes | 0.265v | **0.42x** |

The per-strike noise distribution is heavily right-skewed: p10 0.017v, p50
0.053v, p90 0.712v. A handful of wide-quote strikes that still pass our 10%
gate drag the mean up by a factor of five, so the choice between mean and
median moves the answer across the decision boundary. **An aggregate that
swings from 0.42x to 2.12x on an arbitrary methodological choice is not
evidence of anything.**

This was caught when an independent implementation measured 0.42x against the
1.63x recorded here and flagged the disagreement rather than adopting the
published figure.

The robust finding is not the aggregate at all. It is the **per-strike**
comparison, which is what the shipped `analytics.richness()` actually does:
each strike's residual is compared to that strike's own IV bid-ask half-width,
and scores exactly zero below it. Measured live on 1,070 liquid strikes:

- **230 strikes (21.5%) score non-zero.** 78.5% are inside their own spread.
- The median scoring strike is worth **$3.94 per contract against a $4.00
  median quoted spread.**

That last line is the whole result, and it is stronger than any ratio: even
where richness survives the noise gate, it is worth almost exactly what it
costs to capture. Richness is a tie-breaker among already-viable strikes. It is
not edge, and the aggregate signal-to-noise figure should not be cited.

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
