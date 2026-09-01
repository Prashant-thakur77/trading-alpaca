"""Tests for the market-hours scheduler.

Everything the scheduler touches from outside — the market clock, the
subprocess that runs a cycle, and the passage of time — is injected, so this
whole file runs with no network, no LLM call and no real sleeping.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import (  # noqa: E402
    SLOT_MINUTES,
    Scheduler,
    SchedulerLocked,
    next_slot,
)

UTC = timezone.utc


class FakeClock:
    """Stands in for alpaca's get_clock(). `states` is consumed one per call."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.states:
            return self.states.pop(0)
        return self.states_default

    states_default = None


class ClockState:
    def __init__(self, is_open, next_open=None, next_close=None):
        self.is_open = is_open
        self.next_open = next_open
        self.next_close = next_close


def _open(next_close):
    return ClockState(True, next_open=None, next_close=next_close)


def _closed(next_open):
    return ClockState(False, next_open=next_open, next_close=None)


class FakeRunner:
    """Records every command; returns a queued returncode per call."""

    def __init__(self, returncodes=None, raises=None):
        self.cmds = []
        self.returncodes = list(returncodes or [])
        self.raises = list(raises or [])

    def __call__(self, cmd, timeout=None):
        self.cmds.append(list(cmd))
        if self.raises:
            exc = self.raises.pop(0)
            if exc is not None:
                raise exc
        rc = self.returncodes.pop(0) if self.returncodes else 0

        class P:
            returncode = rc
            stdout = ""
            stderr = ""

        return P()


class FakeSleeper:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


# ── slot alignment ────────────────────────────────────────────

def test_next_slot_is_the_following_aligned_boundary():
    now = datetime(2026, 8, 31, 9, 30, 0, tzinfo=UTC)
    assert next_slot(now, 30) == datetime(2026, 8, 31, 10, 0, 0, tzinfo=UTC)


def test_next_slot_rounds_up_from_mid_interval():
    now = datetime(2026, 8, 31, 9, 47, 12, tzinfo=UTC)
    assert next_slot(now, 30) == datetime(2026, 8, 31, 10, 0, 0, tzinfo=UTC)


def test_slow_cycle_does_not_accumulate_drift():
    """A cycle that overruns its slot skips to the next boundary, it does not
    schedule 30 minutes from when it happened to finish."""
    started = datetime(2026, 8, 31, 9, 30, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=41)  # overran its own slot
    assert next_slot(finished, 30) == datetime(2026, 8, 31, 10, 30, 0, tzinfo=UTC)


def test_a_missed_slot_is_skipped_not_replayed():
    """Airflow's catchup=False. Stale market data must never reach a cycle."""
    finished = datetime(2026, 8, 31, 12, 5, 0, tzinfo=UTC)
    nxt = next_slot(finished, 30)
    assert nxt == datetime(2026, 8, 31, 12, 30, 0, tzinfo=UTC)
    assert nxt > finished


def test_default_slot_is_thirty_minutes():
    assert SLOT_MINUTES == 30


# ── stopping conditions ───────────────────────────────────────

def test_exits_cleanly_once_the_market_closes(tmp_path):
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _closed(close + timedelta(days=1))])
    runner = FakeRunner()
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 15, 59, tzinfo=UTC))
    assert s.run() == 0
    assert len(runner.cmds) == 1, "one cycle while open, then stop"


def test_kill_switch_halts_before_any_cycle_runs(tmp_path):
    ks = tmp_path / "KILL"
    ks.write_text("halt")
    clock = FakeClock([_open(datetime(2026, 8, 31, 16, 0, tzinfo=UTC))])
    runner = FakeRunner()
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=ks, lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    assert s.run() == 0
    assert runner.cmds == [], "kill switch must stop the loop before submitting"


def test_kill_switch_is_rechecked_between_cycles(tmp_path):
    """Armed mid-session, it must stop the NEXT cycle, not only the first."""
    ks = tmp_path / "KILL"
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _open(close), _open(close)])
    clock.states_default = _open(close)

    runner = FakeRunner()
    original = runner.__call__

    def arm_after_first(cmd, timeout=None):
        result = original(cmd, timeout=timeout)
        ks.write_text("halt")
        return result

    s = Scheduler(clock=clock, runner=arm_after_first, sleeper=FakeSleeper(),
                  kill_switch_path=ks, lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    assert s.run() == 0
    assert len(runner.cmds) == 1


# ── resilience ────────────────────────────────────────────────

def test_a_crashing_cycle_does_not_kill_the_scheduler(tmp_path):
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _open(close), _closed(close)])
    runner = FakeRunner(raises=[RuntimeError("boom"), None])
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    assert s.run() == 0
    assert len(runner.cmds) == 2, "second cycle still ran after the first crashed"


def test_a_nonzero_cycle_does_not_stop_the_loop(tmp_path):
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _open(close), _closed(close)])
    runner = FakeRunner(returncodes=[1, 0])
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    assert s.run() == 0
    assert len(runner.cmds) == 2


# ── singleton ─────────────────────────────────────────────────

def test_a_second_scheduler_refuses_to_start(tmp_path):
    """K8s concurrencyPolicy: Forbid. Two schedulers must never both submit."""
    lock = tmp_path / "lock"
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)

    first = Scheduler(clock=FakeClock([_closed(close)]), runner=FakeRunner(),
                      sleeper=FakeSleeper(), kill_switch_path=tmp_path / "KILL",
                      lock_path=lock,
                      now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    first.acquire()
    try:
        second = Scheduler(clock=FakeClock([_closed(close)]), runner=FakeRunner(),
                           sleeper=FakeSleeper(), kill_switch_path=tmp_path / "KILL",
                           lock_path=lock,
                           now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
        with pytest.raises(SchedulerLocked):
            second.acquire()
    finally:
        first.release()


def test_lock_is_released_so_a_later_run_can_start(tmp_path):
    lock = tmp_path / "lock"
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    mk = lambda: Scheduler(  # noqa: E731
        clock=FakeClock([_closed(close)]), runner=FakeRunner(),
        sleeper=FakeSleeper(), kill_switch_path=tmp_path / "KILL", lock_path=lock,
        now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    a = mk(); a.acquire(); a.release()
    b = mk(); b.acquire(); b.release()  # must not raise


# ── the command it builds ─────────────────────────────────────

def test_dry_scheduler_never_passes_live(tmp_path):
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _closed(close)])
    runner = FakeRunner()
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  symbol="SPY", live=False,
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    s.run()
    assert "--live" not in runner.cmds[0]
    assert "--symbol" in runner.cmds[0]
    assert "SPY" in runner.cmds[0]


def test_live_scheduler_passes_live(tmp_path):
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    clock = FakeClock([_open(close), _closed(close)])
    runner = FakeRunner()
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  symbol="SPY", live=True,
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    s.run()
    assert "--live" in runner.cmds[0]


def test_waits_for_the_open_rather_than_running_while_closed(tmp_path):
    """Pre-market: it must sleep toward next_open, not fire a cycle."""
    nopen = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
    clock = FakeClock([_closed(nopen), _open(nopen + timedelta(hours=6))])
    runner = FakeRunner()
    sleeper = FakeSleeper()
    s = Scheduler(clock=clock, runner=runner, sleeper=sleeper,
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  wait_for_open=True,
                  now=lambda: datetime(2026, 8, 31, 12, 0, tzinfo=UTC))
    s.run()
    assert sleeper.slept, "should have slept toward the open"
    assert clock.calls >= 2, "must re-read the clock after waiting, not assume it opened"


def test_closed_market_without_wait_runs_nothing(tmp_path):
    """Default (no --wait-for-open): a closed market means exit, not a cycle."""
    clock = FakeClock([_closed(datetime(2026, 9, 1, 13, 30, tzinfo=UTC))])
    runner = FakeRunner()
    s = Scheduler(clock=clock, runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 22, 0, tzinfo=UTC))
    assert s.run() == 0
    assert runner.cmds == []


def test_a_clock_outage_does_not_crash_the_scheduler(tmp_path):
    """A transient API blip must cost one slot, not the session."""
    close = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)

    class FlakyClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient")
            return _open(close) if self.calls == 2 else _closed(close)

    runner = FakeRunner()
    s = Scheduler(clock=FlakyClock(), runner=runner, sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=tmp_path / "lock",
                  now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    assert s.run() == 0
    assert len(runner.cmds) == 1, "recovered and ran a cycle after the blip"


def test_cycles_run_detached_from_the_schedulers_signal_group():
    """Regression for 2026-08-31 19:38.

    Ctrl+C reached the cycle as well as the scheduler, so run_session.py died
    of KeyboardInterrupt inside the committee while the scheduler was logging
    "finishing the current cycle, then exiting". Dry that costs one slot;
    live, a SIGINT mid-submit abandons an order in an unknown state.
    """
    import inspect

    import scheduler as sched

    src = inspect.getsource(sched._default_runner)
    assert "start_new_session=True" in src, (
        "cycles must run in their own process group, or a terminal Ctrl+C "
        "kills the in-flight cycle and the graceful drain is a lie")


def test_a_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    """Regression for 2026-09-01 16:17.

    Last night's scheduler left .scheduler.lock behind holding PID 250058,
    a process that no longer existed. release() runs in a finally, so a clean
    exit or SIGTERM tidies up — but a SIGKILL, an OOM or a power cut does not.
    The stale file then refuses every later start, which lands precisely when
    someone is trying to launch before the open and can least afford to debug
    a lock file.

    A lock naming a PID that is gone carries no information and must not
    outrank a live scheduler that wants to run.
    """
    lock = tmp_path / "lock"
    lock.write_text("999999\n")          # a PID that cannot be running

    s = Scheduler(clock=FakeClock([_closed(datetime(2026, 9, 1, 13, 30, tzinfo=UTC))]),
                  runner=FakeRunner(), sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=lock,
                  now=lambda: datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    s.acquire()          # must NOT raise
    assert lock.read_text().strip() == str(os.getpid())
    s.release()


def test_a_lock_held_by_a_live_process_is_still_refused(tmp_path):
    """The reclaim must not become a way for two schedulers to both run."""
    lock = tmp_path / "lock"
    lock.write_text(f"{os.getpid()}\n")   # this test process is very much alive

    s = Scheduler(clock=FakeClock([_closed(datetime(2026, 9, 1, 13, 30, tzinfo=UTC))]),
                  runner=FakeRunner(), sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=lock,
                  now=lambda: datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    with pytest.raises(SchedulerLocked):
        s.acquire()


def test_an_unreadable_lock_is_refused_not_reclaimed(tmp_path):
    """Garbage in the lock means we cannot prove the holder is dead, so the
    safe reading is that it is alive."""
    lock = tmp_path / "lock"
    lock.write_text("not-a-pid\n")

    s = Scheduler(clock=FakeClock([_closed(datetime(2026, 9, 1, 13, 30, tzinfo=UTC))]),
                  runner=FakeRunner(), sleeper=FakeSleeper(),
                  kill_switch_path=tmp_path / "KILL", lock_path=lock,
                  now=lambda: datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    with pytest.raises(SchedulerLocked):
        s.acquire()
