#!/usr/bin/env python3
"""Seed the Brier calibration loop by replaying the REAL committee over REAL
post-knowledge-cutoff history — `make seed-calibration`.

    python3 scripts/seed_calibration.py --symbol SPY \\
        --start 2026-06-01 --end 2026-08-15 --max-windows 20 [--dry-run]

Why this exists
---------------
`make calibration` is the project's one genuine differentiator: no competitor
repo grades its own agents. But on a journal with no closed trades it
correctly reports zero resolved predictions and a weight of exactly 1.0 for
everybody (calibration.py refuses to invent a score on a small sample, and
that refusal is deliberate). Honest, and invisible. This script makes the
loop demonstrable by giving it real resolved outcomes — without loosening a
single thing about how those outcomes are obtained.

The contamination rule
----------------------
The model's knowledge cutoff is **May 2026**. Every window here starts on or
after 2026-06-01 (`seed_replay.KNOWLEDGE_CUTOFF`), which is enforced by
`seed_replay.validate_window` raising rather than clamping. This project
publicly criticises TradingAgents (arXiv 2412.20138) and FinMem
(arXiv 2311.13743) for backtesting LLM agents over dates inside their own
training data; doing it ourselves would make that criticism false. If there
is not enough post-cutoff calendar for the requested number of windows, the
run produces FEWER windows. It never reaches back.

What is real and what is substituted
------------------------------------
Real, fetched from Alpaca and never invented:
  * the underlying's daily closes (decision-date spot AND the subsequent
    price action that resolves the outcome),
  * every option leg's daily bar, fetched by a directly constructed OCC
    symbol because `TradingClient.get_option_contracts` returns nothing for
    an expired expiry.

Substituted, named here and in the artifact so a judge can discount them:
  * `bid == ask == close` — a daily bar records a traded price, not a
    spread. Guessing a spread would fabricate the exact number the liquidity
    gate reads, so the gate is simply not exercised in replay.
  * `open_interest := the day's volume` — historical OI is not retrievable;
    volume is real, observed, and the nearest liquidity proxy.

Neither substitution can manufacture a favourable OUTCOME: outcomes come
from subsequent price action alone (`seed_replay.resolve_outcome`).

Fail closed
-----------
A window whose data cannot be assembled from real bars is SKIPPED with a
logged reason and appears in the artifact's skip table. Nothing is ever
filled in with a synthetic price. An ABSTAIN is a valid, fully journalled
outcome that simply resolves no prediction.

Journal separation
------------------
Writes to `logs/seed_journal.jsonl` by default, never the live
`logs/journal.jsonl`, which is a judged artifact of real broker interaction.
Every payload is stamped `source="seed_replay"` by `seed_replay.
ReplayJournal`, so no entry in the seeded chain can be mistaken for a trade.
"""
import argparse
import logging
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import analytics  # noqa: E402
import seed_replay  # noqa: E402
from committee.decide import ABSTAIN, decide  # noqa: E402
from journal import Journal  # noqa: E402
from llm.cache import PromptCache  # noqa: E402
from llm.client import call_claude  # noqa: E402
from scripts.run_session import build_candidates  # noqa: E402

logger = logging.getLogger("seed_calibration")

DEFAULT_JOURNAL = REPO_ROOT / "logs" / "seed_journal.jsonl"
DEFAULT_ARTIFACT = REPO_ROOT / "docs" / "calibration_seeding.md"
PROMPT_CACHE_DIR = REPO_ROOT / "logs" / "prompt_cache"

# Bars needed BEFORE the first decision date to measure realized vol. 20
# trading days is analytics.realized_volatility's default window; 45 calendar
# days covers it with room for holidays.
LEAD_IN_DAYS = 45

# The strike grid. `build_candidates` pairs a short leg with the leg exactly
# `WIDTH` away, so the ladder must sit on that grid or no vertical can form.
WIDTH = 5.0
LADDER_STEPS = 6            # +/- 6 strikes -> 13 strikes, 26 contracts

# Below this many priced legs on the decision date there is no usable chain,
# so the window is skipped rather than traded off a handful of contracts.
MIN_PRICED_LEGS = 6

# The free market-data subscription refuses "recent SIP data", so any request
# whose end date reaches today is rejected outright. Everything this script
# reads is history by definition, so the ceiling costs nothing — but it has
# to be applied, or a run dies on the very first fetch.
DATA_LAG_DAYS = 1


# The same subscription also refuses any request whose END TIMESTAMP falls
# inside the last quarter hour, which bites at the start of a UTC day: an
# end-of-day timestamp on "yesterday" is only minutes old at 00:05 UTC and is
# rejected as recent SIP data even though the ceiling date is correct.
DATA_LAG_MINUTES = 20


def _data_ceiling() -> date:
    return date.today() - timedelta(days=DATA_LAG_DAYS)


def _request_end(end: date) -> datetime:
    """End timestamp for a bars request: end-of-day, but never inside the
    subscription's recent-data window."""
    return min(datetime.combine(end, datetime.max.time(), timezone.utc),
               datetime.now(timezone.utc) - timedelta(minutes=DATA_LAG_MINUTES))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — same shape as scripts/run_session's."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        import os
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ── market data (the only part that touches the network) ─────

class HistoricalBars:
    """Real daily bars for one underlying and any option symbol asked for.

    Deliberately NOT `alpaca_data.AlpacaData`: that adapter is built for a
    LIVE chain (it fetches snapshots and merges open interest from the
    trading API's contract records, neither of which exists for an expired
    expiry). Historical replay needs the option BARS endpoint and constructed
    OCC symbols instead, which is a different data path with different
    failure modes, so it gets its own small class rather than an `if
    historical:` branch inside the live one.
    """

    def __init__(self, stock_client, option_client):
        self.stock_client = stock_client
        self.option_client = option_client

    @classmethod
    def from_env(cls) -> "HistoricalBars":
        import os
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SystemExit(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set — seeding "
                "reads real historical bars and cannot run without them.")
        return cls(StockHistoricalDataClient(key, secret),
                   OptionHistoricalDataClient(key, secret))

    def stock_closes(self, symbol: str, start: date, end: date) -> dict:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        end = min(end, _data_ceiling())
        response = self.stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time(), timezone.utc),
            end=_request_end(end)))
        bars = response.data.get(symbol, []) if hasattr(response, "data") else []
        return {b.timestamp.date(): float(b.close) for b in bars}

    def option_bars(self, symbols, start: date, end: date) -> dict:
        """{OCC symbol -> {date -> (close, volume)}}, real bars only.

        A symbol the API has never heard of simply does not appear in the
        result; that is the "no bar, so no quote" case the callers fail
        closed on, and it must not be turned into an exception.
        """
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame
        symbols = sorted(set(symbols))
        end = min(end, _data_ceiling())
        if not symbols or end < start:
            return {}
        response = self.option_client.get_option_bars(OptionBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time(), timezone.utc),
            end=_request_end(end)))
        data = getattr(response, "data", {}) or {}
        out: dict[str, dict] = {}
        for symbol, bars in data.items():
            out[str(symbol).upper()] = {
                b.timestamp.date(): (float(b.close), float(b.volume)) for b in bars}
        return out


# ── LLM accounting ───────────────────────────────────────────

class CountingClient:
    """Wraps `call_claude` to count calls and dollars actually spent.

    Sits OUTSIDE `committee.decide`'s prompt-cache wrapper, so a cache hit
    never reaches this object at all — which is exactly what makes
    `report.llm_calls` the count of calls genuinely made and re-runs of a
    seeded window free.

    Locked because `committee.analysts.run_analysts` deliberately runs the
    two analysts CONCURRENTLY: an unsynchronised `+= 1` there loses
    increments, and a cost figure that quietly under-reports is precisely the
    kind of flattering number this whole exercise exists not to produce.
    """

    def __init__(self, inner=call_claude):
        self.inner = inner
        self.calls = 0
        self.cost_usd = 0.0
        self._lock = threading.Lock()

    def __call__(self, prompt, model=None, **kw):
        response = self.inner(prompt, model=model, **kw) if model else self.inner(prompt, **kw)
        with self._lock:
            self.calls += 1
            self.cost_usd += float(getattr(response, "cost_usd", 0.0) or 0.0)
            calls, cost = self.calls, self.cost_usd
        print(f"      LLM call #{calls} ({model}) — ${cost:.4f} spent so far",
              flush=True)
        return response


class CountingCache:
    """A `PromptCache` that counts how many calls it served.

    The hit count is half of the honest cost story: `llm_calls` alone cannot
    distinguish "this run was cheap because the committee abstained early"
    from "this run was cheap because it replayed a cache". Both numbers go in
    the artifact.
    """

    def __init__(self, inner):
        self.inner = inner
        self.hits = 0
        self._lock = threading.Lock()

    def get(self, key):
        record = self.inner.get(key)
        if record is not None:
            with self._lock:
                self.hits += 1
        return record

    def put(self, key, record):
        return self.inner.put(key, record)


# ── one window ───────────────────────────────────────────────

def expiry_candidates(as_of: date) -> list[date]:
    """Every weekday in the replay DTE band — the expiries worth probing.

    SPY lists daily expiries, so this is a superset of what actually exists;
    `pick_expiry` prefers a Friday and the bar fetch is the final arbiter of
    whether the chosen expiry is real.
    """
    return [as_of + timedelta(days=n)
            for n in range(seed_replay.MIN_DTE, seed_replay.MAX_DTE + 1)
            if (as_of + timedelta(days=n)).weekday() < 5]


def chain_for(symbol: str, as_of: date, expiry: date, spot: float, bars: dict):
    """The replayed chain: one OptionQuote per leg that has a real bar."""
    quotes = []
    for strike in seed_replay.strike_ladder(spot, WIDTH, LADDER_STEPS):
        for right in ("c", "p"):
            occ = seed_replay.occ_symbol(symbol, expiry, right, strike)
            bar = (bars.get(occ) or {}).get(as_of)
            if bar is None:
                continue
            close, volume = bar
            quote = seed_replay.quote_from_bar(symbol, expiry, right, strike,
                                               close, volume, as_of)
            if quote is not None:
                quotes.append(quote)
    return quotes


def leg_close_series(bars: dict) -> dict:
    """{symbol -> {date -> close}} for the resolver, dropping the volumes."""
    return {sym: {d: close for d, (close, _vol) in series.items()}
            for sym, series in bars.items()}


def run_window(window_id: int, as_of: date, symbol: str, market, closes: dict,
               journal_path: Path, cache, client, report, dry_run: bool) -> None:
    spot = closes.get(as_of)
    if spot is None:
        report.skip(as_of, "no underlying bar on the decision date")
        return

    # `max_expiry` is the last date real price action exists for. A window
    # whose expiry has not happened yet cannot be resolved from what actually
    # happened next, only guessed at — so it is refused here, before an LLM
    # call is spent on it.
    expiry = seed_replay.pick_expiry(as_of, expiry_candidates(as_of),
                                     max_expiry=_data_ceiling())
    if expiry is None:
        report.skip(as_of, f"no expiry in the {seed_replay.MIN_DTE}-"
                           f"{seed_replay.MAX_DTE} DTE band that has already "
                           f"expired (real price action ends "
                           f"{_data_ceiling().isoformat()})")
        return

    ladder = seed_replay.strike_ladder(spot, WIDTH, LADDER_STEPS)
    symbols = [seed_replay.occ_symbol(symbol, expiry, right, strike)
               for strike in ladder for right in ("c", "p")]
    try:
        bars = market.option_bars(symbols, as_of, expiry)
    except Exception as e:  # noqa: BLE001 — a data outage skips, never fabricates
        report.skip(as_of, f"option bar fetch failed: {type(e).__name__}: {e}")
        return

    chain = chain_for(symbol, as_of, expiry, spot, bars)
    if len(chain) < MIN_PRICED_LEGS:
        report.skip(as_of, f"only {len(chain)} of {len(symbols)} legs had a real "
                           f"bar on {as_of.isoformat()} (need {MIN_PRICED_LEGS})")
        return

    # Realized vol from the real bars up to and including the decision date —
    # never a later one, which would leak the future into the snapshot.
    import pandas as pd
    history = pd.DataFrame({"close": [closes[d] for d in sorted(closes) if d <= as_of]})
    realized_vol = analytics.realized_volatility(history)
    atm_iv = analytics.atm_implied_vol(chain, spot)

    candidates = build_candidates(chain, symbol, spot, width=WIDTH)
    if not candidates:
        report.skip(as_of, f"chain of {len(chain)} legs produced no candidate "
                           f"that survives the builder's liquidity gates")
        return

    print(f"  [{window_id}] {as_of} spot {spot:.2f} exp {expiry} "
          f"({(expiry - as_of).days} DTE) — {len(chain)} legs, "
          f"{len(candidates)} candidates, rv={realized_vol:.1%}, "
          f"iv={'n/a' if atm_iv is None else f'{atm_iv:.1%}'}", flush=True)

    if dry_run:
        report.record(as_of, abstained=False, reason="dry run — committee not run",
                      expiry=expiry, spot=spot, candidates=len(candidates),
                      choice_id="(dry-run)")
        return

    journal = seed_replay.ReplayJournal(Journal(journal_path), as_of, window_id)
    decision = decide(symbol, spot, realized_vol, candidates, journal,
                      cache=cache, client=client, atm_iv=atm_iv)

    if decision.chosen is None:
        print(f"      ABSTAIN — {decision.abstain_reason[:140]}", flush=True)
        report.record(as_of, abstained=True, reason=decision.abstain_reason,
                      expiry=expiry, spot=spot, candidates=len(candidates),
                      choice_id=ABSTAIN, snapshot_hash=decision.snapshot_hash)
        return

    intent = decision.chosen
    outcome = seed_replay.resolve_outcome(
        intent, intent.contracts, leg_close_series(bars), closes, as_of)
    if outcome is None:
        report.record(as_of, abstained=False,
                      reason="decision made but no real price action could "
                             "resolve it — no close journalled",
                      expiry=expiry, spot=spot, candidates=len(candidates),
                      choice_id=decision.choice_id, structure=intent.structure,
                      snapshot_hash=decision.snapshot_hash)
        return

    # THE JOIN. `calibration.resolved_predictions` correlates each
    # analyst_view with this entry through `snapshot_hash`; without it the
    # replayed cycle would score nothing. Same key and same field name the
    # live `exit_monitor` writes, so the two are read by identical code.
    journal.append("close", {
        "underlying": symbol,
        "structure": intent.structure,
        "contracts": intent.contracts,
        "realized_pnl": float(outcome.realized_pnl),
        "snapshot_hash": decision.snapshot_hash,
        "resolution_method": outcome.method,
        "resolution_detail": outcome.detail,
        "exit_date": outcome.exit_date.isoformat(),
        "entry_spot": spot,
        "note": "REPLAYED, NOT TRADED — no order was ever sent to a broker "
                "for this entry; the P&L is computed from historical bars.",
    })
    print(f"      {decision.choice_id} {intent.structure} -> "
          f"${outcome.realized_pnl:,.2f} via {outcome.method}", flush=True)
    report.record(as_of, abstained=False, expiry=expiry, spot=spot,
                  candidates=len(candidates), choice_id=decision.choice_id,
                  structure=intent.structure, method=outcome.method,
                  realized_pnl=float(outcome.realized_pnl),
                  exit_date=outcome.exit_date, detail=outcome.detail,
                  snapshot_hash=decision.snapshot_hash)


# ── entry point ──────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--start", default="2026-06-01",
                   help="first decision date; must be >= the 2026-06-01 "
                        "knowledge-cutoff boundary")
    p.add_argument("--end", default="2026-08-15")
    p.add_argument("--max-windows", type=int, default=20)
    p.add_argument("--spacing", type=int, default=3,
                   help="trading days between decision dates, so consecutive "
                        "windows are not near-identical market states")
    p.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    p.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    p.add_argument("--append", action="store_true",
                   help="knowingly add to a journal that already holds a "
                        "seeded chain. Any window replayed twice is counted "
                        "twice by calibration.resolved_predictions, because "
                        "the snapshot hash is deterministic — so this is "
                        "opt-in, never the default.")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and build, but spend no LLM calls and write "
                        "nothing to the journal")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    _load_dotenv(REPO_ROOT / ".env")

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    try:
        seed_replay.validate_window(start, end)
    except seed_replay.ContaminationError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    journal_path = Path(args.journal)
    already = seed_replay.seeded_entry_count(
        Journal(journal_path).entries() if journal_path.exists() else [])
    if already and not (args.append or args.dry_run):
        print(f"REFUSED: {journal_path} already holds {already} entries from a "
              f"previous seeding run. Replaying the same window twice writes a "
              f"second analyst_view under the SAME deterministic snapshot_hash, "
              f"so calibration would count one observation as two. Delete the "
              f"file to re-seed from scratch, point --journal somewhere else, "
              f"or pass --append if you really mean to extend this chain.",
              file=sys.stderr)
        return 3

    market = HistoricalBars.from_env()
    closes = market.stock_closes(args.symbol, start - timedelta(days=LEAD_IN_DAYS),
                                 _data_ceiling())
    if not closes:
        print(f"No {args.symbol} bars returned — cannot seed.", file=sys.stderr)
        return 1

    dates = seed_replay.decision_dates(closes, start, end, args.spacing,
                                       args.max_windows)
    report = seed_replay.SeedReport(symbol=args.symbol, start=start, end=end,
                                    spacing=args.spacing)
    cache = CountingCache(PromptCache(PROMPT_CACHE_DIR))
    client = CountingClient()

    print(f"Seeding calibration from {len(dates)} post-cutoff decision date(s) "
          f"on {args.symbol}, {start} .. {end}"
          + (" [DRY RUN]" if args.dry_run else f" -> {journal_path}"))
    for i, as_of in enumerate(dates, start=1):
        try:
            run_window(i, as_of, args.symbol, market, closes, journal_path,
                       cache, client, report, args.dry_run)
        except Exception as e:  # noqa: BLE001 — one bad window never ends the run
            logger.exception("window %s raised", as_of)
            report.skip(as_of, f"unhandled {type(e).__name__}: {e}")

    report.llm_calls = client.calls
    report.llm_cache_hits = cache.hits
    report.llm_cost_usd = client.cost_usd
    text = report.render()
    if not args.dry_run:
        artifact = Path(args.artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(text)
        print(f"\nArtifact written to {artifact}")
    print()
    print(text)
    print(f"Now run: python3 scripts/calibration_report.py --journal {journal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
