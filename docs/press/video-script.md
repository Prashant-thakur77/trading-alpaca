# Demo video — the script as recorded

Runtime **3:55**, 1920×1080, 25 fps. Screen recording with synthesised voiceover; no face.

**Rule the recording follows:** every number spoken is on screen in the same scene, and every
on-screen terminal is real log text — no mock-ups, no retyped output. Sources per scene are listed.
Video was cut to the narration length per scene, so audio and picture are in sync by construction.

Built with Playwright + Chromium (recording) and Chatterbox TTS (voice); the build files live
alongside the video and are reproducible from the logs in this repo.

## 00:00 – 00:25 · hero

**On screen:** https://trading-alpaca-judge.vercel.app/

> Most trading agents ask a language model what to buy, and act on the answer. That is fast to build, and impossible to trust. The model can hallucinate a strike, size a position wrongly, and be confidently wrong with no record of why. This desk inverts that relationship. Deterministic code builds every legal, defined-risk options trade first. The model's only power is to pick one by number, or to refuse.

## 00:25 – 00:53 · fill

**On screen:** terminal — *make schedule-live  —  Tuesday 1 Sep, live paper account*  
**Source:** `logs/scheduler-20260901.log`, the 19:28 IST cycle (Tuesday's fill) — verbatim
**Footer:** `broker: FILLED at $2.35 net credit against a $1.94 limit  (order 2026-09-01 14:01 UTC)`

> This is a real cycle on the official paper account. It read a live chain and built one thousand eight hundred and fifty two fully specified candidates before any model was called. Then the committee voted. The volatility analyst put the trade at seventy nine percent. The adversary put it at fifty. The trader chose, both veto gates passed, the guard allowed it, and the order filled. At a two dollar thirty five credit, against a one ninety four limit. Better than we asked for.

## 00:53 – 01:20 · committee

**On screen:** terminal — *the committee — the adversary changed the trade*  
**Source:** `logs/scheduler-20260901.log`, the 19:28 IST cycle (Tuesday's fill) — verbatim

> Read what actually happened between the analysts. The adversary objected that the highest credit candidate's short strike was essentially at the money, with high assignment risk if realized volatility spiked. The trader answered that specific objection. It moved to a strike one point one percent out of the money, and said so: more room to absorb the adversary's scenario. The adversary changed the trade. That is the difference between a committee and a rubber stamp.

## 01:20 – 01:46 · veto

**On screen:** terminal — *two veto gates — and they disagreed*  
**Source:** `logs/scheduler-20260903.log`, the cycle whose blind review vetoed — verbatim

> Every pick then faces two independent gates. The first is pure code: does the position's own delta match the structure's thesis? Here it passed. The second is a blind review by a separate model, shown only the trade and the market, never the analysts' arguments. Here it vetoed. The first gate cannot catch what the second caught, and the desk did nothing. Abstaining is a first class outcome, not a failure.

## 01:46 – 02:11 · close

**On screen:** terminal — *Thursday 3 Sep — the exit monitor fires*  
**Source:** `logs/scheduler-20260903.log`, the 21:00 IST cycle, plus the `close` entry from `logs/journal.jsonl`

> Before every entry, a pre-mortem writes deterministic exit triggers into the journal. On Thursday, SPY rallied through one of them: the breakeven at seven seventy two point four two. The monitor closed the spread on its own, for a realized loss of seventy dollars fifty. It took a small, defined loss by its own rule, before it could grow toward the maximum. That is the risk system working, not failing.

## 02:11 – 02:42 · calibration

**On screen:** terminal — *make calibration — it grades its own analysts*  
**Source:** `python3 scripts/calibration_report.py --journal logs/seed_journal.jsonl` — verbatim output

> Every analyst carries a track record. Each probability is scored against what actually happened, with the Brier score. Over twelve resolved replayed trades, the volatility analyst scores point one six two and is upweighted to one point one eight. The adversary scores point two nine eight and is demoted to point nine. Read that honestly: a systematic pessimist scores badly on a winning sample by construction, and its real value was the vetoes it landed, which Brier does not measure. Twelve outcomes is a first signal, not a verdict.

## 02:42 – 03:02 · verify

**On screen:** terminal — *env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY  python3 scripts/replay.py --all --verify*  
**Source:** `env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY python3 scripts/replay.py --all --verify` and `make verify-journal` — verbatim output

> You do not have to trust any of this. Four recorded decisions replay with no credentials, no API keys, and no model calls, and all four verdicts recompute to match. Every decision, including every refusal, is appended to a hash chained journal. Three fills. One close. Chain intact.

## 03:02 – 03:14 · judge

**On screen:** https://trading-alpaca-judge.vercel.app/judge

> The judge page puts the same four scenarios one click away. Two of the four are refusals, deliberately. A judge can step through every stage of a real decision without asking us for anything.

## 03:14 – 03:55 · honest

**On screen:** terminal — *official window — what actually happened*  
**Source:** broker account state at the Thursday close, and the four bug-fix commits in `git log`

> What it does not claim. This is a paper account, and four trades prove nothing statistically. The official window closed at ninety nine thousand eight hundred twenty seven dollars, down point one seven percent, every position inside its defined risk. All three trades were bear call spreads, and SPY rallied. One view, expressed three times. We also found four real bugs during the live sessions, every one while the test suite was green, and every one only visible because the thing was actually running. We fixed each with a test and shipped it mid session. A desk that can refuse, cut its own losers, grade its own analysts, and show you its mistakes. Paper trading only.

---

## What was deliberately left out

- No music. It reads as marketing under technical content.
- No sped-up terminals. The typewriter pace is tuned to the narration, not to look fast.
- No claim of edge. The closing number is stated as it is, with the reason.
- The `%,` logging traceback that appears at the top of the 21:00 cycle is cropped, not hidden:
  it is a cosmetic format-string bug, noted in the repo, and the close event that follows it is shown in full.
