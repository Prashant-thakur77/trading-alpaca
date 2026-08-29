"""
Alpaca market data adapter — stock bars and option chains, 15-minute cached.

Replaces kraken_data.py. Clients are injected so the whole adapter is testable
with no credentials and markets closed.

One non-obvious thing this module exists to handle: Alpaca's option-chain
snapshots carry quotes, IV and Greeks but **no open interest**, while open
interest lives on the contract records from the trading API. risk.yaml gates on
OI >= 100, so `get_option_chain` merges the two sources and drops any contract
whose OI cannot be established. Unknown OI becomes 0, which fails the liquidity
gate — fail closed rather than trade blind (hard rule 2).

Failure policy follows spec 4.4 — **fail loud on data, fail soft on the LLM**:

  * `get_stock_bars` returns an empty frame on failure; its caller treats an
    empty frame as a hard stop, so the distinction is already visible there.
  * `get_option_chain` **raises MarketDataError** when a fetch fails, and
    returns [] only when the fetch succeeded and nothing was tradeable. These
    are opposite situations — an outage versus a market judgement — and
    collapsing them into [] let a broken feed be reported as "ABSTAIN: no
    candidate passed the liquidity gate" with a success exit code.

Never partial or stale data, and failures are not cached.

Every network call is wrapped in `_with_timeout`. alpaca-py exposes no request
timeout of its own — `RESTClient` accepts only `retry_attempts`,
`retry_wait_seconds` and `retry_exception_codes` — so a stalled socket blocks
the caller indefinitely. That is not theoretical: a seeding run hung inside
alpaca-py and had to be killed by hand. A session that hangs during market
hours is worse than one that fails, because a hang is silent and a failure is
loud.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from candidate_builder import MAX_DTE, MIN_DTE, OptionQuote

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 15 * 60  # 15 minutes

# Generous enough for a slow option-chain page, short enough that a scan cycle
# cannot stall past its own 30-minute schedule.
DEFAULT_REQUEST_TIMEOUT = 45


def _with_timeout(fn, *args, timeout: int = DEFAULT_REQUEST_TIMEOUT, **kwargs):
    """Run a blocking client call under a wall-clock ceiling.

    Raises TimeoutError when the ceiling is hit. Reaching it is treated as a
    failed fetch, never as empty data.

    A daemon thread rather than ThreadPoolExecutor: the executor's context
    manager calls shutdown(wait=True) on exit, so it blocks on the very thread
    it is supposed to be timing out, and its worker threads are non-daemon, so
    interpreter shutdown would join them too. A daemon thread abandons the
    stalled socket and lets the process exit.
    """
    box: dict = {}

    def _run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:      # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e

    worker = threading.Thread(target=_run, daemon=True,
                              name=f"alpaca-{getattr(fn, '__name__', 'request')}")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(
            f"{getattr(fn, '__name__', 'request')} exceeded {timeout}s"
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


class MarketDataError(RuntimeError):
    """A market-data fetch failed. Not the same as "nothing to trade"."""


class AlpacaData:
    """Bars + option chains with a TTL cache.

    Clients are injected for testability; `from_env` builds the real ones.
    """

    def __init__(self, stock_client, option_client, trading_client, clock=time.monotonic,
                 request_timeout: int = DEFAULT_REQUEST_TIMEOUT):
        self.stock_client = stock_client
        self.option_client = option_client
        self.trading_client = trading_client
        self._clock = clock
        self.request_timeout = request_timeout
        self._cache: dict[tuple, tuple[float, object]] = {}

    @classmethod
    def from_env(cls) -> "AlpacaData":
        """Build from ALPACA_API_KEY / ALPACA_SECRET_KEY. Paper endpoints only."""
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — see .env.example"
            )
        return cls(
            stock_client=StockHistoricalDataClient(key, secret),
            option_client=OptionHistoricalDataClient(key, secret),
            trading_client=TradingClient(key, secret, paper=True),
        )

    # ── cache ────────────────────────────────────────────────
    def _cached(self, key: tuple):
        hit = self._cache.get(key)
        if hit is None:
            return None
        stamped_at, value = hit
        if self._clock() - stamped_at > CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        return value

    def _store(self, key: tuple, value) -> None:
        """Cache only non-empty results, so a failed fetch is retried."""
        if value is None:
            return
        if hasattr(value, "empty") and value.empty:
            return
        if isinstance(value, (list, dict)) and not value:
            return
        self._cache[key] = (self._clock(), value)

    # ── stock bars ───────────────────────────────────────────
    def get_stock_bars(self, symbol: str, days: int = 90, timeframe=None) -> pd.DataFrame:
        """Daily OHLCV bars. Returns an empty frame on any failure."""
        key = ("bars", symbol, days)
        hit = self._cached(key)
        if hit is not None:
            return hit

        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe or TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days),
            )
            response = _with_timeout(self.stock_client.get_stock_bars, request,
                                     timeout=self.request_timeout)
            bars = response.data.get(symbol, []) if hasattr(response, "data") else []
        except Exception as e:
            logger.warning("Bar fetch failed for %s: %s", symbol, e)
            return pd.DataFrame()

        if not bars:
            logger.warning("No bars returned for %s", symbol)
            return pd.DataFrame()

        df = pd.DataFrame([{
            "timestamp": b.timestamp,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        } for b in bars])

        self._store(key, df)
        return df

    # ── option chain ─────────────────────────────────────────
    def get_option_chain(
        self, underlying: str, min_dte: int = MIN_DTE, max_dte: int = MAX_DTE
    ) -> list[OptionQuote]:
        """Option chain as OptionQuotes, with open interest merged in.

        Raises MarketDataError if either fetch fails — the caller must stop and
        say so, not abstain quietly. Returns [] when the fetches succeeded but
        no contract survived: contracts whose OI or quote cannot be established
        are dropped rather than guessed at.
        """
        key = ("chain", underlying, min_dte, max_dte)
        hit = self._cached(key)
        if hit is not None:
            return hit

        today = datetime.now(timezone.utc).date()
        try:
            from alpaca.data.requests import OptionChainRequest
            snapshots = _with_timeout(
                self.option_client.get_option_chain,
                OptionChainRequest(
                    underlying_symbol=underlying,
                    expiration_date_gte=today + timedelta(days=min_dte),
                    expiration_date_lte=today + timedelta(days=max_dte),
                ),
                timeout=self.request_timeout,
            ) or {}
        except Exception as e:
            logger.error("Option chain fetch failed for %s: %s", underlying, e)
            raise MarketDataError(f"Option chain fetch failed for {underlying}: {e}") from e

        open_interest = self._open_interest_by_symbol(underlying, min_dte, max_dte)
        if not open_interest:
            # A successful call that returned no contracts: an empty universe,
            # not an outage. Every snapshot then fails the OI merge below and
            # the result is an honest empty chain.
            logger.warning("No contract metadata for %s — cannot verify open interest", underlying)
            return []

        quotes: list[OptionQuote] = []
        for symbol, snap in snapshots.items():
            meta = open_interest.get(symbol)
            if meta is None:
                # No contract record — open interest unverifiable. Drop it.
                continue
            quote = getattr(snap, "latest_quote", None)
            if quote is None:
                continue
            bid, ask = getattr(quote, "bid_price", None), getattr(quote, "ask_price", None)
            if bid is None or ask is None:
                continue

            quotes.append(OptionQuote(
                symbol=symbol,
                underlying=underlying,
                strike=float(meta["strike"]),
                expiry=meta["expiry"],
                right=meta["right"],
                bid=float(bid),
                ask=float(ask),
                open_interest=int(meta["open_interest"] or 0),
            ))

        self._store(key, quotes)
        return quotes

    def _open_interest_by_symbol(self, underlying: str, min_dte: int, max_dte: int) -> dict:
        """Contract metadata keyed by OCC symbol: strike, expiry, right, OI."""
        today = datetime.now(timezone.utc).date()
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            response = _with_timeout(
                self.trading_client.get_option_contracts,
                GetOptionContractsRequest(
                    underlying_symbols=[underlying],
                    expiration_date_gte=today + timedelta(days=min_dte),
                    expiration_date_lte=today + timedelta(days=max_dte),
                    limit=10_000,
                ),
                timeout=self.request_timeout,
            )
            contracts = getattr(response, "option_contracts", None) or []
        except Exception as e:
            logger.error("Contract metadata fetch failed for %s: %s", underlying, e)
            raise MarketDataError(
                f"Contract metadata fetch failed for {underlying}: {e}"
            ) from e

        out = {}
        for c in contracts:
            right = getattr(c.type, "value", c.type)
            out[c.symbol] = {
                "strike": c.strike_price,
                "expiry": c.expiration_date,
                # Alpaca says "call"/"put"; OptionQuote uses "c"/"p".
                "right": str(right).lower()[0],
                "open_interest": c.open_interest,
            }
        return out
