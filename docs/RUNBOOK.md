# Live session runbook

Market opens **09:30 ET = 19:00 IST**. Everything below is copy-pasteable.

## Before the bell

```bash
cd /home/prashant/trading-alpaca
set -a; . ./.env; set +a          # without this the MCP server 401s and the CLI fails
make check-account                 # must print "Ready to trade."
```

`make check-account` verifies the things that silently ruin a session: the keys
are paper keys, options level is 3 (level 2 cannot trade spreads at all), the
account is flat, the CLI is on PATH, and the market clock.

If it does not say **Ready to trade**, stop and read the FAIL line. It names the
fix.

## The session itself

```bash
make session          # SAFE. Runs the whole pipeline, sends nothing.
make session-live     # SUBMITS. Requires the explicit --live flag.
```

`make session` is deliberately the safe one. On 2026-08-29 a shell-expanded
backtick in an unrelated `git commit -m` ran exactly this target and submitted a
real paper order nobody intended. It now cannot.

**Run `make session` first, every time.** It prints the committee's reasoning,
both veto results, the guard verdict, the position Greeks and the exact payload
that would be sent. Read it. Only then run `make session-live`.

## What you should expect to see

A normal cycle prints, in order: the market snapshot, the candidate count, the
two analysts with their probabilities, the trader's choice, both vetoes, the
guard verdict, and the payload. Roughly 30-60 seconds, most of it real LLM
latency.

**An ABSTAIN is a normal, successful outcome and exits 0.** Measured over 43
historical windows the desk abstained 72% of the time. Seven of eight recent
refusals were the two reviewers failing to agree on direction, which is the
system working. Do not treat an abstention as a failure to be worked around.

## If it abstains and you want a fill

Legitimate options, in order of preference:

1. **Wait and run the next cycle.** The menu and the regime change through the day.
2. **Try a different underlying:** `python3 scripts/run_session.py --symbol QQQ`.
   The per-underlying daily cap is per symbol, so this is not a way around a limit.
3. **Read the reason it gave.** If it cites a structural gap rather than
   conviction, that is a bug worth knowing about, not something to override.

**Do not** lower a risk limit, widen a gate, or disable a veto to force a trade.
Every limit in `risk.yaml` was set before we knew what it would block, which is
the only time such a number can be set honestly.

## If something breaks

| Symptom | Cause | Action |
|---|---|---|
| `401 unauthorized` | `.env` not exported | `set -a; . ./.env; set +a` |
| Session hangs | was an `alpaca-py` hang with no timeout | now capped at 45s; it will fail loudly instead |
| `DATA FETCH FAILED` | a real outage | correct behaviour. It refuses to guess. Retry later |
| Claude rate limit | subscription throttling | `python3 scripts/run_session.py --live --no-llm` runs the deterministic selector inside the same RiskGuard |
| Order submitted by accident | | see kill switch below, then cancel in the Alpaca dashboard |

## Kill switch

```bash
touch KILL_SWITCH        # halts everything, immediately
rm KILL_SWITCH           # resume
```

Or `KILL=1` in the environment. Checked at startup **and** again before every
order, so a session already running stops before its next submission. It
resolves relative to `risk.yaml`, not the working directory, so it works under
cron from anywhere.

## After a fill — capture the evidence

This is the artifact almost nobody in the field has. Capture it while it is live.

```bash
make verify-journal                 # chain must report INTACT
python3 scripts/calibration_report.py
```

Screenshot: the terminal showing the filled order, and the Alpaca dashboard
showing the position. Save both to `docs/evidence/`.

## End of the day

```bash
python3 scripts/run_session.py --live --close-all   # if positions are open and you want flat
make verify-journal
```

Leaving a position open overnight is fine and is what the exit monitor is for.
The forced close at 3 DTE exists so a short ITM leg is never assigned into stock
at expiry; do not disable it to hold a position longer.
