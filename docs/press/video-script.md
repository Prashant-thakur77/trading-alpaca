# Demo video — script and shot list

Target 2:45, hard ceiling 4:00. Screen recording with voiceover; no face needed.
Record at 1920x1080. Every number spoken must be on screen at the same moment.

**Before you start recording**
- `set -a; . ./.env; set +a` in every terminal, or the MCP server 401s on camera.
- Browser tabs open in this order: the live site, `/judge`, `/smile`, the Alpaca
  paper dashboard showing the filled order.
- Terminal font large enough to read at 1080p. Clear scrollback.
- Run `make check-account` once beforehand so the cache is warm and it returns fast.
- Have `logs/journal.jsonl` non-empty from Monday's live session.

---

## 00:00 - 00:20 · The problem, stated as a refusal

**On screen:** the live site hero, "The desk that grades itself."

> Most trading agents ask a language model what to buy, and act on the answer.
> That is quick to build and impossible to trust. The model can hallucinate a
> strike, size a position wrongly, and be confidently wrong with no record of why.
> This one inverts that. Deterministic code builds every trade. The model's only
> power is to choose one, or refuse.

Scroll slowly to the facts strip so 3,686 → 632 → 12 → 1 lands under the voice.

## 00:20 - 00:55 · A real filled order

**On screen:** terminal, `make session` then the Alpaca dashboard order.

> Here is a real multi-leg options order on a paper account. Not a mock, not a
> backtest.

Show the payload: `"order_class": "mleg"`, `"limit_price": "-1.78"`.

> That negative limit price is Alpaca's convention for a net credit. The
> `client_order_id` is derived from the legs and the UTC date, so a retry is
> rejected by the broker as a duplicate rather than opening a second position.

Cut to the dashboard showing the fill. **Hold on it for three full seconds** —
this is the artifact almost nobody in the field has.

## 00:55 - 01:35 · The committee, including a refusal

**On screen:** terminal, `make session` output, the committee block.

> Two analysts argue. A volatility analyst weighs implied against realized vol.
> A dedicated adversary argues against every trade and looks for the failure mode.
> Each returns a probability, not a verdict.

Point at the two probabilities on screen.

> Here the adversary objected that the breakevens were only a fraction of a
> percent away. The trader moved off the highest-credit candidate to answer that
> specific objection. The adversary changed the trade. That is the difference
> between a committee and a rubber stamp.

**If you have an ABSTAIN cycle recorded, show it here instead of a second trade.**

> And when they disagree, the desk does nothing. Abstaining is a first-class
> outcome, not a failure.

## 01:35 - 02:10 · It grades its own analysts

**On screen:** `make calibration` against the seeded journal.

> Every analyst carries a track record. Each prediction is scored against what
> actually happened, with the standard Brier score. The volatility analyst scores
> 0.162. The adversary scores 0.298. The better-calibrated analyst is upweighted
> to 1.18; the worse one is demoted to 0.90.

**Say the caveat out loud. Do not skip this.**

> Read that honestly. The book was ten and two, and a systematic pessimist scores
> badly on a winning sample by construction. The adversary's real value was the
> three vetoes it landed, which the Brier score does not measure at all. Twelve
> outcomes is a first signal, not a verdict.

## 02:10 - 02:30 · Verify it yourself

**On screen:** terminal, credentials stripped.

    env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY python3 scripts/replay.py --all --verify

> Four recorded decisions, replayed with no credentials, no API keys and no model
> calls. The verdicts are recomputed here, not read back.

Let all four `OK` lines appear. Then:

    make verify-journal

> And the decision chain verifies. Every decision, including every refusal, is
> hash-chained.

Cut to `/judge` in the browser. Click the **fail-closed** tab.

> Two of the four scenarios are refusals. A judge can click through them without
> asking us for anything.

## 02:30 - 02:45 · What it does not claim

**On screen:** the "what we cannot prove" section of the site.

> Paper trading only. Thirty out-of-sample trades proves nothing statistically,
> and one symbol loses money, which is the point. An earlier version of our own
> harness scaled its risk threshold to the wrong horizon and produced a 97% win
> rate by construction. We caught it and corrected it before publishing.
> The repository this was converted from shipped a hardcoded 82.2% win rate
> computed from nothing. We deleted it rather than adapt it.

End on the site with the URL readable.

---

## Rules for the recording

- **Never say a number that is not on screen.** If it is not visible, cut it.
- **Do not speed up the terminal.** Real latency is credibility; a 9-second LLM
  call looks like a real system.
- **Do not hide the abstention.** It is the most differentiating thing here.
- If Monday's session abstains rather than fills, **record that honestly** and
  say why — a desk that refuses on a thin menu is a better story than a staged
  fill, and the fill can come from a later session.
- No music under the technical sections. It reads as marketing.

## If a take goes wrong

The two most likely failures on camera are an `alpaca-py` hang (now capped at 45
seconds, so it will fail rather than freeze) and a Claude rate limit. If the
committee cannot be reached, `make session` still runs with `--no-llm` and the
deterministic selector, and saying so on camera is better than cutting to a
prepared clip.
