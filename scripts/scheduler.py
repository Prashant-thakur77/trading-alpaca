#!/usr/bin/env python3
"""Market-hours scheduler: run one session cycle per aligned slot, RTH only.

    make schedule        # dry, submits nothing
    make schedule-live   # armed, can submit real paper orders

Why this exists: a single cycle reaches an executable trade about 28% of the
time (12 of 43 replayed windows). Across a full session of 30-minute slots
that compounds to ~99%, so the difference between "probably no fill today"
and "almost certainly a fill" is simply running the cycle repeatedly.

`run_session.py` has no clock of its own, so the scheduler owns market hours.
It runs the session as a SUBPROCESS rather than importing it: a cycle that
crashes, hangs, or exits non-zero must never take the scheduler down with it.

Design borrowed from schedulers that had to get this right:

  * cron / systemd timers — slots are aligned to the wall clock (09:30,
    10:00, 10:30), so a slow cycle never pushes every later one later.
  * Airflow `catchup=False` — a missed slot is skipped, never replayed.
    Firing a cycle on stale market data is worse than skipping it.
  * Quartz misfire policy "do nothing" — the same rule at slot level.
  * Kubernetes CronJob `concurrencyPolicy: Forbid` — a PID lock, so two
    schedulers can never both be submitting.
  * Temporal graceful drain — SIGINT/SIGTERM finishes the cycle in flight
    and then stops, so a signal can never land mid-order.

Safety: submitting requires BOTH --live and --i-understand-this-submits-real-orders.
One flag is a typo; two is a decision. Double-opening is already impossible —
RiskGuard enforces max_new_per_underlying_per_day, so after a SPY fill the
later cycles manage the open book instead of opening another.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION = REPO_ROOT / "scripts" / "run_session.py"

SLOT_MINUTES = 30
# A cycle builds ~1500 candidates and makes several model calls; 15 minutes is
# generous. Exceeding it means something is wrong, and the slot is lost anyway.
CYCLE_TIMEOUT_SECONDS = 900
# Never sleep longer than this in one go, so a kill switch armed while the
# scheduler waits for the open is noticed within a minute rather than hours.
MAX_SLEEP_CHUNK_SECONDS = 60

log = logging.getLogger("scheduler")


class SchedulerLocked(RuntimeError):
    """Another scheduler already holds the lock."""


def next_slot(now: datetime, interval_minutes: int = SLOT_MINUTES) -> datetime:
    """The next wall-clock-aligned boundary strictly after `now`.

    Aligned to the hour, so with a 30-minute interval the slots are :00 and
    :30 regardless of when the scheduler started or how long a cycle took.
    Always strictly in the future: a cycle that overran its own slot resumes
    at the next boundary rather than immediately, and a slot missed entirely
    is skipped rather than replayed.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    anchor = now.replace(minute=0, second=0, microsecond=0)
    elapsed = (now - anchor).total_seconds() / 60.0
    steps = int(elapsed // interval_minutes) + 1
    return anchor + timedelta(minutes=steps * interval_minutes)


def kill_switch_reason(path: Path) -> str | None:
    """Non-None if trading must halt. Mirrors risk_guard's two triggers."""
    if os.environ.get("KILL") == "1":
        return "env KILL=1"
    if Path(path).exists():
        return f"{path} present"
    return None


class Scheduler:
    def __init__(self, *, clock, runner=None, sleeper=None, kill_switch_path,
                 lock_path, symbol: str = "SPY", live: bool = False,
                 interval_minutes: int = SLOT_MINUTES,
                 cycle_timeout: int = CYCLE_TIMEOUT_SECONDS,
                 wait_for_open: bool = False, now=None):
        self.clock = clock
        self.runner = runner if runner is not None else _default_runner
        self.sleeper = sleeper if sleeper is not None else time.sleep
        self.kill_switch_path = Path(kill_switch_path)
        self.lock_path = Path(lock_path)
        self.symbol = symbol
        self.live = live
        self.interval_minutes = interval_minutes
        self.cycle_timeout = cycle_timeout
        self.wait_for_open = wait_for_open
        self.now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._stopping = False
        self._locked = False

    # ── singleton ────────────────────────────────────────────
    def acquire(self) -> None:
        """Take the lock, or raise. O_EXCL makes the check-and-create atomic,
        so two schedulers racing at the open cannot both win."""
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SchedulerLocked(
                f"another scheduler holds {self.lock_path}. If it is not "
                f"running, delete that file.") from None
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        self._locked = True

    def release(self) -> None:
        if self._locked:
            self.lock_path.unlink(missing_ok=True)
            self._locked = False

    # ── signals ──────────────────────────────────────────────
    def request_stop(self, *_args) -> None:
        """Drain rather than abort: the cycle in flight finishes first."""
        log.warning("stop requested — finishing the current cycle, then exiting")
        self._stopping = True

    # ── the loop ─────────────────────────────────────────────
    def run(self) -> int:
        while not self._stopping:
            reason = kill_switch_reason(self.kill_switch_path)
            if reason:
                log.warning("HALTED — kill switch: %s", reason)
                return 0

            try:
                state = self.clock()
            except Exception as e:  # noqa: BLE001 — never die on a clock blip
                log.warning("could not read the market clock (%s); retrying", e)
                if not self._sleep_until(self.now() + timedelta(minutes=1)):
                    return 0
                continue

            if state is None:
                return 0

            if not getattr(state, "is_open", False):
                if not self.wait_for_open:
                    log.info("market is closed — nothing more to do today")
                    return 0
                nopen = getattr(state, "next_open", None)
                if nopen is None:
                    return 0
                log.info("market closed; waiting for the open at %s", nopen)
                if not self._sleep_until(nopen):
                    return 0
                # Loop round and re-read the clock rather than assuming it opened.
                self.wait_for_open = False
                continue

            self._run_one_cycle()

            if self._stopping:
                break
            if not self._sleep_until(next_slot(self.now(), self.interval_minutes)):
                return 0
        return 0

    def _run_one_cycle(self) -> None:
        cmd = [sys.executable, str(SESSION), "--symbol", self.symbol]
        if self.live:
            cmd.append("--live")
        log.info("cycle: %s", " ".join(cmd[1:]))
        try:
            proc = self.runner(cmd, timeout=self.cycle_timeout)
        except Exception as e:  # noqa: BLE001
            # A cycle that dies is one lost slot, not a lost session.
            log.error("cycle failed (%s: %s) — continuing to the next slot",
                      type(e).__name__, e)
            return
        rc = getattr(proc, "returncode", None)
        if rc not in (0, None):
            log.warning("cycle exited %s — continuing to the next slot", rc)

    def _sleep_until(self, when: datetime) -> bool:
        """Sleep in short chunks. False if we must stop instead of continuing.

        Chunked so that a kill switch armed, or a signal sent, while the
        scheduler is idling is acted on within a minute — a scheduler that
        cannot be stopped between slots is not safe to leave running.
        """
        while True:
            if self._stopping:
                return False
            reason = kill_switch_reason(self.kill_switch_path)
            if reason:
                log.warning("HALTED — kill switch: %s", reason)
                return False
            remaining = (when - self.now()).total_seconds()
            if remaining <= 0:
                return True
            self.sleeper(min(remaining, MAX_SLEEP_CHUNK_SECONDS))
            # A stubbed sleeper does not advance a stubbed clock; without this
            # the loop would spin forever under test.
            if (when - self.now()).total_seconds() >= remaining:
                return True


def _default_runner(cmd, timeout=None):
    return subprocess.run(cmd, cwd=str(REPO_ROOT), timeout=timeout)


def _default_clock():
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    from alpaca.trading.client import TradingClient

    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    client = TradingClient(key, secret, paper=True)
    return client.get_clock()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--interval-minutes", type=int, default=SLOT_MINUTES)
    p.add_argument("--wait-for-open", action="store_true",
                   help="if the market is closed, wait for the open instead of exiting")
    p.add_argument("--live", action="store_true",
                   help="submit real paper orders (requires the flag below too)")
    p.add_argument("--i-understand-this-submits-real-orders", action="store_true",
                   dest="confirmed",
                   help="second required flag for --live. One flag is a typo; "
                        "two is a decision.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")

    if args.live and not args.confirmed:
        print("\n  --live also requires --i-understand-this-submits-real-orders."
              "\n  Refusing to start armed on a single flag.\n", file=sys.stderr)
        return 2

    s = Scheduler(clock=_default_clock, kill_switch_path=REPO_ROOT / "KILL_SWITCH",
                  lock_path=REPO_ROOT / ".scheduler.lock", symbol=args.symbol,
                  live=args.live, interval_minutes=args.interval_minutes,
                  wait_for_open=args.wait_for_open)

    signal.signal(signal.SIGINT, s.request_stop)
    signal.signal(signal.SIGTERM, s.request_stop)

    mode = "LIVE — orders WILL be submitted" if args.live else "dry — nothing is submitted"
    log.info("scheduler starting: %s, %s, every %d min",
             args.symbol, mode, args.interval_minutes)
    try:
        s.acquire()
    except SchedulerLocked as e:
        print(f"\n  {e}\n", file=sys.stderr)
        return 2
    try:
        return s.run()
    finally:
        s.release()
        log.info("scheduler stopped")


if __name__ == "__main__":
    sys.exit(main())
