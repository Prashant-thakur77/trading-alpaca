#!/usr/bin/env python3
# scripts/replay.py
"""
The credential-free judge replay engine (design spec 4.6).

A judge must be able to replay a real desk decision with NO credentials, NO
network, and NO LLM call — and verify the recomputed outputs match what was
recorded, byte for byte. That reproducibility is what makes the judge page
trustworthy rather than a slideshow.

    python3 scripts/replay.py --list
    python3 scripts/replay.py --scenario allow
    python3 scripts/replay.py --all --json
    python3 scripts/replay.py --verify

Each fixture in judge/scenarios/*.json captures one complete real (or, where
marked in its `provenance`, honestly constructed) committee cycle: the market
snapshot, the surfaced candidates, each analyst's recorded view, the trader's
recorded choice, both veto results, the guard's verdict and the resulting
order payload.

This module re-runs only the DETERMINISTIC half of that cycle — thesis_check,
RiskGuard.evaluate, and the order payload builder — against the fixture's
inputs, using the fixture's recorded LLM outputs (analyst views, trader
choice, blind-review verdict) verbatim. It never calls an LLM and never opens
a network connection: every function it imports below is pure code, plus
py_vollib's local Black-Scholes math. `fail_closed` is the one exception in
shape, not in spirit — it replays an upstream DATA outage by re-running
`scripts/run_session.main` (the real production entrypoint) against injected
fakes, which is exactly as network-free as everything else here.

Exit code of a single `--scenario` (or `--verify`) run is 0 when the replay
reproduces the recorded outcome, 1 when it diverges or the fixture cannot be
loaded at all.
"""
import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from analytics import position_greeks  # noqa: E402
from candidate_builder import (  # noqa: E402
    OptionQuote,
    build_bear_call_spread,
    build_bear_put_spread,
    build_bull_call_spread,
    build_bull_put_spread,
    build_iron_condor,
    build_long_iron_butterfly,
    build_long_straddle,
)
from committee.analysts import AnalystView, aggregate  # noqa: E402
from committee.veto import thesis_check  # noqa: E402
from options_orders import build_mleg_payload, client_order_id  # noqa: E402
from risk_guard import PortfolioState, RiskGuard, load_risk_config  # noqa: E402

SCENARIOS_DIR = REPO_ROOT / "judge" / "scenarios"
STAGES = ("snapshot", "committee", "veto", "guard", "execution")


class ScenarioError(Exception):
    """A fixture is missing, corrupt, or structurally invalid.

    Raised instead of guessing: a judge-facing replay that silently rendered
    a best-effort reading of a broken fixture would be worse than one that
    refuses outright — see the module docstring's reproducibility guarantee.
    """


@dataclass
class ReplayResult:
    """The outcome of replaying one scenario."""
    scenario: str
    fixture: dict
    trace: dict = field(default_factory=dict)
    matched: bool = False
    mismatches: list = field(default_factory=list)


# ── fixture I/O ──────────────────────────────────────────────

def list_scenarios(scenarios_dir: Path | None = None) -> list[str]:
    directory = scenarios_dir if scenarios_dir is not None else SCENARIOS_DIR
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def load_fixture(name: str, scenarios_dir: Path | None = None) -> dict:
    """Load and minimally validate one fixture. Never returns a half-read or
    guessed-at structure — anything wrong here is a hard failure."""
    directory = scenarios_dir if scenarios_dir is not None else SCENARIOS_DIR
    path = directory / f"{name}.json"
    if not path.exists():
        raise ScenarioError(f"no such scenario {name!r} (looked for {path})")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ScenarioError(f"scenario {name!r} at {path} is corrupt JSON: {e}") from e
    if not isinstance(data, dict):
        raise ScenarioError(f"scenario {name!r} at {path} is not a JSON object")
    if data.get("scenario") != name:
        raise ScenarioError(
            f"scenario file {path} declares scenario={data.get('scenario')!r}, "
            f"expected {name!r} — refusing a mismatched fixture"
        )
    if "provenance" not in data or "recorded_outcome" not in data:
        raise ScenarioError(
            f"scenario {name!r} at {path} is missing 'provenance' or "
            f"'recorded_outcome' — refusing an incomplete fixture"
        )
    return data


# ── the fixture's own calendar ───────────────────────────────

def fixture_as_of(name: str, fixture: dict) -> date:
    """The date this fixture's market snapshot was OBSERVED on.

    Every DTE-dependent gate in the desk — the 7-45 DTE window, the IV solve
    behind the Greeks, the pre-mortem's 3-DTE exit — is derived from
    `(expiry - as_of).days`. A replay that measured the fixture's expiries
    against *today's* calendar would be answering a different question every
    day, so the observation date is read from the fixture and never from the
    clock.

    `order_date` (present since the fixtures were first written, and the key
    `client_order_id` is derived from) is accepted as a fallback because on
    every committed fixture the desk observed the chain and cut the order on
    the same session. They stay separate fields because they mean different
    things: `as_of` is when the market was seen, `order_date` is the UTC day
    the broker-side idempotency key belongs to.
    """
    raw = fixture.get("as_of") or fixture.get("order_date")
    if not raw:
        raise ScenarioError(
            f"{name}: fixture has neither 'as_of' nor 'order_date' — refusing "
            f"to date its option chain from the system clock")
    try:
        return date.fromisoformat(str(raw))
    except ValueError as e:
        raise ScenarioError(f"{name}: as_of {raw!r} is not an ISO date: {e}") from e


# ── rebuilding a real TradeIntent from a fixture candidate ──

def candidate_expiry(candidate: dict, as_of: date) -> date:
    """The candidate's ABSOLUTE expiry, read from the fixture — never
    reconstructed from the clock.

    `dte` is kept in the fixtures for human readability, but it is derived
    and cross-checked here rather than trusted: a candidate whose two
    representations of the same fact disagree is a corrupt fixture, and this
    is a judged artifact, so it fails loudly instead of picking one.
    """
    cid = candidate.get("id")
    raw = {str(candidate["expiry"])} if candidate.get("expiry") else set()
    raw |= {str(leg["expiry"]) for leg in candidate.get("legs") or []
            if leg.get("expiry")}
    if not raw:
        raise ScenarioError(
            f"candidate {cid!r} carries no absolute 'expiry' — refusing to "
            f"anchor it to today's date, which would make this replay's OCC "
            f"symbols drift one day per calendar day")
    if len(raw) > 1:
        raise ScenarioError(
            f"candidate {cid!r} declares conflicting expiries {sorted(raw)}")
    try:
        expiry = date.fromisoformat(raw.pop())
    except ValueError as e:
        raise ScenarioError(f"candidate {cid!r} has a non-ISO expiry: {e}") from e

    if "dte" in candidate:
        declared = int(candidate["dte"])
        derived = (expiry - as_of).days
        if declared != derived:
            raise ScenarioError(
                f"candidate {cid!r} is self-inconsistent: expiry {expiry} is "
                f"{derived} days after as_of {as_of}, but dte says {declared}")
    return expiry


def _quote(leg: dict, underlying: str, expiry: date, as_of: date) -> OptionQuote:
    """One fixture leg as a real `OptionQuote`, stamped with the date it was
    observed. `as_of` is what keeps `OptionQuote.dte` — and therefore every
    gate derived from it — equal to what was recorded, on any future day."""
    strike = float(leg["strike"])
    symbol = leg.get("symbol") or (
        f"{underlying}{expiry:%y%m%d}{leg['right'].upper()}{int(strike * 1000):08d}"
    )
    return OptionQuote(
        symbol=symbol, underlying=underlying, strike=strike, expiry=expiry,
        right=leg["right"], bid=float(leg["bid"]), ask=float(leg["ask"]),
        open_interest=int(leg["open_interest"]), as_of=as_of,
    )


def rebuild_intent(candidate: dict, underlying: str, contracts: int, as_of: date):
    """Reconstruct the real `TradeIntent` for one fixture candidate via the
    REAL `candidate_builder` functions — never by hand-assembling the
    dataclass — so every liquidity gate and structural invariant the live
    builder enforces is enforced here too. Returns `None` if the candidate
    fails that gate on rebuild (a corrupted or self-inconsistent fixture).

    Both dates come from the FIXTURE: the absolute `expiry` it records, and
    the `as_of` the chain was observed on. Each quote is stamped with `as_of`,
    so `OptionQuote.dte` — and every gate derived from it — reproduces the
    recorded value on any future day, while the OCC symbols reproduce the
    recorded contracts exactly.

    This used to anchor `expiry = date.today() + dte`, which held `dte`
    steady but let the reconstructed expiry advance one day per calendar day.
    From the day after the fixtures were generated, the recomputed OCC symbols
    stopped matching the recorded ones (SPY261001C00777000 vs the recorded
    SPY260930C00777000) and every trade scenario reported MISMATCH — on the
    one page whose entire purpose is verification.
    """
    try:
        structure = candidate["structure"]
        legs = candidate["legs"]
    except (KeyError, TypeError, ValueError) as e:
        raise ScenarioError(f"candidate {candidate.get('id')!r} is malformed: {e}") from e

    expiry = candidate_expiry(candidate, as_of)

    def by(side=None, right=None):
        return [dict(leg) for leg in legs
                if (side is None or leg.get("side") == side)
                and (right is None or leg.get("right") == right)]

    try:
        def q(leg):
            return _quote(leg, underlying, expiry, as_of)

        if structure == "bear_call_spread":
            (short,), (long_,) = by("sell"), by("buy")
            return build_bear_call_spread(q(short), q(long_), contracts=contracts)
        if structure == "bull_put_spread":
            (short,), (long_,) = by("sell"), by("buy")
            return build_bull_put_spread(q(short), q(long_), contracts=contracts)
        if structure == "bull_call_spread":
            (long_,), (short,) = by("buy"), by("sell")
            return build_bull_call_spread(q(long_), q(short), contracts=contracts)
        if structure == "bear_put_spread":
            (long_,), (short,) = by("buy"), by("sell")
            return build_bear_put_spread(q(long_), q(short), contracts=contracts)
        if structure == "long_straddle":
            (call,), (put,) = by(right="c"), by(right="p")
            return build_long_straddle(q(call), q(put), contracts=contracts)
        if structure == "long_iron_butterfly":
            (long_call,), (long_put,) = by("buy", "c"), by("buy", "p")
            (short_call,), (short_put,) = by("sell", "c"), by("sell", "p")
            return build_long_iron_butterfly(
                long_call=q(long_call), long_put=q(long_put),
                short_call=q(short_call), short_put=q(short_put),
                contracts=contracts)
        if structure == "iron_condor":
            (short_put,), (long_put,) = by("sell", "p"), by("buy", "p")
            (short_call,), (long_call,) = by("sell", "c"), by("buy", "c")
            return build_iron_condor(q(short_put), q(long_put),
                                     q(short_call), q(long_call),
                                     contracts=contracts)
    except ValueError as e:  # a genuinely malformed leg set for the structure
        raise ScenarioError(f"candidate {candidate.get('id')!r} legs invalid: {e}") from e

    raise ScenarioError(f"candidate {candidate.get('id')!r} has unknown structure {structure!r}")


# ── comparison helpers ───────────────────────────────────────

def _round_floats(obj, ndigits=4):
    """Round every float in a JSON-shaped structure, so a legitimate
    float-formatting difference (e.g. -0.0 vs 0.0) never reads as a
    mismatch. Structural differences still compare unequal."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _intent_json(intent) -> dict:
    return {
        "underlying": intent.underlying, "structure": intent.structure,
        "dte": intent.dte, "contracts": intent.contracts,
        "net_credit": round(intent.net_credit, 4),
        "max_loss": round(intent.max_loss, 4),
        "max_profit": None if intent.max_profit == float("inf") else round(intent.max_profit, 4),
        "breakevens": [round(b, 4) for b in intent.breakevens],
    }


# ── the trade-scenario replay (allow / deny / downsize) ─────

def _replay_trade(name: str, fixture: dict) -> ReplayResult:
    result = ReplayResult(scenario=name, fixture=fixture)
    mismatches = result.mismatches

    market = fixture.get("market") or {}
    underlying = market.get("underlying")
    spot = market.get("spot")
    if not underlying or spot is None:
        raise ScenarioError(f"{name}: fixture market block is missing underlying/spot")

    candidates_list = fixture.get("candidates") or []
    by_id = {c["id"]: c for c in candidates_list}
    chosen_id = fixture.get("chosen_id")
    if chosen_id not in by_id:
        raise ScenarioError(
            f"{name}: chosen_id {chosen_id!r} is not among this fixture's candidates")
    chosen_cand = by_id[chosen_id]
    requested_contracts = int(fixture.get("requested_contracts", 1))
    as_of = fixture_as_of(name, fixture)

    # ── stage 1: snapshot ─────────────────────────────────────
    result.trace["snapshot"] = {
        "ok": True,
        "as_of": as_of.isoformat(),
        "underlying": underlying, "spot": spot,
        "realized_vol": market.get("realized_vol"), "atm_iv": market.get("atm_iv"),
        "candidate_count": len(candidates_list),
        "candidate_ids": list(by_id),
    }

    # ── stage 2: committee (recorded LLM output, not re-called) ─
    committee = fixture.get("committee") or {}
    views_data = committee.get("views") or []
    recomputed_agg = aggregate([
        AnalystView(role=v["role"], probability=v.get("probability"),
                    abstained=bool(v.get("abstained", False)),
                    abstain_reason=v.get("abstain_reason", ""),
                    reasoning=v.get("reasoning", ""), model=v.get("model", ""),
                    prompt_hash="")
        for v in views_data
    ])
    trader = committee.get("trader") or {}
    result.trace["committee"] = {
        "reached": True,
        "views": views_data,
        "aggregate_probability": recomputed_agg,
        "trader_choice_id": trader.get("choice_id"),
        "trader_reasoning": trader.get("reasoning", ""),
    }
    recorded_agg = committee.get("aggregate_probability")
    if recorded_agg is not None and recomputed_agg is not None \
            and round(recorded_agg, 6) != round(recomputed_agg, 6):
        mismatches.append(
            f"aggregate_probability: recomputed {recomputed_agg} != recorded {recorded_agg}")
    if trader.get("choice_id") != chosen_id:
        mismatches.append(
            f"trader.choice_id {trader.get('choice_id')!r} != fixture chosen_id {chosen_id!r}")

    # ── rebuild the chosen TradeIntent via REAL candidate_builder ─
    intent = rebuild_intent(chosen_cand, underlying, requested_contracts, as_of)
    if intent is None:
        raise ScenarioError(
            f"{name}: candidate {chosen_id!r} failed the liquidity gate on rebuild "
            f"— fixture is inconsistent with the live builder")

    # ── stage 3: veto — thesis_check recomputed fresh, blind veto
    # is the fixture's recorded LLM output ────────────────────
    thesis_ok, thesis_reason = thesis_check(intent, spot)
    veto = fixture.get("veto") or {}
    blind = veto.get("blind") or {}
    result.trace["veto"] = {
        "reached": True,
        "thesis": {"ok": thesis_ok, "reason": thesis_reason},
        "blind": {"ok": bool(blind.get("ok")), "reason": blind.get("reason", ""),
                  "model": blind.get("model")},
    }
    recorded_thesis = (fixture.get("recorded_outcome") or {}).get("thesis") or {}
    if recorded_thesis and bool(recorded_thesis.get("ok")) != thesis_ok:
        mismatches.append(
            f"thesis_check: recomputed ok={thesis_ok} != recorded ok={recorded_thesis.get('ok')}")

    # ── stage 4: guard — recomputed fresh, always ────────────
    measured = position_greeks(intent, spot)
    if measured is None:
        raise ScenarioError(f"{name}: position Greeks unmeasurable on rebuild")
    pos_delta, pos_vega = measured

    ps = fixture.get("portfolio_state") or {}
    try:
        state = PortfolioState(
            open_positions=int(ps["open_positions"]), net_delta=float(ps["net_delta"]),
            net_vega=float(ps["net_vega"]), daily_realized_pnl=float(ps["daily_realized_pnl"]),
            consecutive_losses=int(ps["consecutive_losses"]),
            new_today_by_underlying=dict(ps.get("new_today_by_underlying") or {}),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ScenarioError(f"{name}: portfolio_state malformed: {e}") from e

    guard = RiskGuard(load_risk_config())
    verdict = guard.evaluate(intent, state, pos_delta, pos_vega)
    result.trace["guard"] = {
        "reached": True,
        "decision": verdict.decision.value, "reason": verdict.reason,
        "approved_contracts": verdict.approved_contracts,
        "position_delta": round(pos_delta, 4), "position_vega": round(pos_vega, 4),
    }

    recorded = fixture.get("recorded_outcome") or {}
    recorded_guard = recorded.get("guard") or {}
    if recorded_guard.get("decision") != verdict.decision.value:
        mismatches.append(
            f"guard.decision: recomputed {verdict.decision.value!r} != "
            f"recorded {recorded_guard.get('decision')!r}")
    if recorded_guard.get("reason") != verdict.reason:
        mismatches.append(
            f"guard.reason: recomputed {verdict.reason!r} != "
            f"recorded {recorded_guard.get('reason')!r}")
    if recorded_guard.get("approved_contracts") != verdict.approved_contracts:
        mismatches.append(
            f"guard.approved_contracts: recomputed {verdict.approved_contracts} != "
            f"recorded {recorded_guard.get('approved_contracts')}")

    # ── stage 5: execution — the order payload, if any ───────
    payload = None
    if verdict.is_tradeable and verdict.approved_contracts >= 1:
        approved_intent = intent if verdict.approved_contracts == intent.contracts \
            else rebuild_intent(chosen_cand, underlying,
                                verdict.approved_contracts, as_of)
        if approved_intent is None:
            raise ScenarioError(
                f"{name}: downsized candidate failed the liquidity gate on rebuild")
        payload = build_mleg_payload(approved_intent, verdict.approved_contracts)
        order_date_raw = fixture.get("order_date")
        # Always pass `on=` explicitly: `client_order_id` otherwise defaults
        # to the UTC date of the run, which would make the id — and so the
        # recorded payload — differ on every replay.
        payload["client_order_id"] = client_order_id(
            approved_intent,
            on=date.fromisoformat(order_date_raw) if order_date_raw else as_of)

    result.trace["execution"] = {"reached": True, "payload": payload}

    recorded_payload = recorded.get("payload")
    if _round_floats(payload) != _round_floats(recorded_payload):
        mismatches.append(
            f"payload: recomputed {payload} != recorded {recorded_payload}")

    result.matched = not mismatches
    return result


# ── the fail-closed scenario replay ──────────────────────────

_ERROR_TYPES = {
    "ConnectionError": ConnectionError,
    "TimeoutError": TimeoutError,
    "RuntimeError": RuntimeError,
    "OSError": OSError,
}


def _replay_fail_closed(name: str, fixture: dict) -> ReplayResult:
    """Re-run the real `scripts/run_session.main` production entrypoint
    against injected fakes whose option-chain fetch raises the fixture's
    recorded error — the same mechanism as
    tests/test_run_session_main.py::TestDataFailures. No network, no
    credentials: the CLI and data adapters are never constructed for real."""
    import contextlib
    import io
    import tempfile

    import pandas as pd
    from datetime import datetime, timedelta as _td, timezone

    import run_session  # imported lazily: only this scenario needs it
    from journal import Journal

    result = ReplayResult(scenario=name, fixture=fixture)
    injected = fixture.get("injected_failure") or {}
    error_cls = _ERROR_TYPES.get(injected.get("error_type"))
    if error_cls is None:
        raise ScenarioError(
            f"{name}: unknown injected_failure.error_type {injected.get('error_type')!r}")
    error_message = injected.get("error_message", "")

    class _FakeCLI:
        def available(self):
            return True

        def get_account(self):
            return {"options_trading_level": 3, "equity": "100000", "last_equity": "100000"}

        def list_positions(self):
            return []

        def list_orders(self, status="open", limit=500):
            return []

    class _FakeData:
        def get_stock_bars(self, symbol, days=30):
            return pd.DataFrame([
                {"timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc) + _td(days=i),
                 "open": 769.0, "high": 771.0, "low": 767.0, "close": 769.35,
                 "volume": 1_000_000}
                for i in range(30)
            ])

        def get_option_chain(self, symbol):
            raise error_cls(error_message)

    argv = list((fixture.get("session_args") or {}).get("argv", ["--symbol", "SPY"]))
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        journal = Journal(Path(td) / "journal.jsonl")
        with contextlib.redirect_stdout(buf):
            exit_code = run_session.main(argv, cli=_FakeCLI(), data=_FakeData(), journal=journal)
    stdout = buf.getvalue()

    result.trace = {
        "snapshot": {"ok": False, "exit_code": exit_code, "stdout": stdout,
                     "injected_failure": injected},
        "committee": {"reached": False},
        "veto": {"reached": False},
        "guard": {"reached": False},
        "execution": {"reached": False, "payload": None},
    }

    recorded = fixture.get("recorded_outcome") or {}
    mismatches = []
    if recorded.get("exit_code") != exit_code:
        mismatches.append(f"exit_code: recomputed {exit_code} != recorded {recorded.get('exit_code')}")
    if recorded.get("stdout") != stdout:
        mismatches.append(f"stdout: recomputed {stdout!r} != recorded {recorded.get('stdout')!r}")

    result.mismatches = mismatches
    result.matched = not mismatches
    return result


# ── public entry points ──────────────────────────────────────

def replay_scenario(name: str, scenarios_dir: Path | None = None) -> ReplayResult:
    """Load one fixture and replay it. Raises `ScenarioError` on a missing or
    corrupt fixture; otherwise always returns a `ReplayResult` (matched may
    be False — that is a reportable outcome, not an exception)."""
    fixture = load_fixture(name, scenarios_dir)
    if fixture["scenario"] == "fail_closed":
        return _replay_fail_closed(name, fixture)
    return _replay_trade(name, fixture)


def verify_all(scenarios_dir: Path | None = None) -> dict:
    """Replay every committed scenario. Returns {name: (matched, mismatches)}.
    A scenario that fails to load at all is reported as a mismatch, never
    raised past this point — this is the function both `--verify` and the
    test suite call to check the whole committed set at once."""
    out = {}
    for name in list_scenarios(scenarios_dir):
        try:
            result = replay_scenario(name, scenarios_dir)
            out[name] = (result.matched, list(result.mismatches))
        except ScenarioError as e:
            out[name] = (False, [str(e)])
    return out


def render_json(name: str, scenarios_dir: Path | None = None) -> str:
    """Deterministic JSON rendering of one scenario's replay — same input,
    byte-identical output, every time."""
    result = replay_scenario(name, scenarios_dir)
    return json.dumps({
        "scenario": result.scenario,
        "provenance": result.fixture.get("provenance"),
        "matched": result.matched,
        "mismatches": result.mismatches,
        "trace": result.trace,
    }, indent=2, sort_keys=True, default=str)


# ── human-readable trace printing ────────────────────────────

def _print_trace(result: ReplayResult) -> None:
    trace = result.trace
    print(f"scenario: {result.scenario}")
    prov = result.fixture.get("provenance") or {}
    print(f"provenance: {prov.get('kind', '(unspecified)')}")
    print()

    snap = trace.get("snapshot", {})
    print("1. SNAPSHOT")
    if snap.get("ok", True):
        print(f"   {snap.get('underlying')} spot ${snap.get('spot'):,.2f} — "
              f"{snap.get('candidate_count')} candidate(s): "
              f"{', '.join(snap.get('candidate_ids', []))}")
    else:
        print(f"   FAILED (exit {snap.get('exit_code')}) — see injected_failure")
        for line in snap.get("stdout", "").splitlines():
            print(f"   | {line}")

    print("2. COMMITTEE")
    committee = trace.get("committee", {})
    if committee.get("reached"):
        for v in committee.get("views", []):
            if v.get("abstained"):
                print(f"   {v['role']:<16} ABSTAINED — {v.get('abstain_reason')}")
            else:
                print(f"   {v['role']:<16} p={v.get('probability'):.2f} — {v.get('reasoning')}")
        print(f"   aggregate probability: {committee.get('aggregate_probability')}")
        print(f"   trader: {committee.get('trader_choice_id')} — "
              f"{committee.get('trader_reasoning')}")
    else:
        print("   not reached")

    print("3. VETO")
    veto = trace.get("veto", {})
    if veto.get("reached"):
        t, b = veto["thesis"], veto["blind"]
        print(f"   thesis: {'PASS' if t['ok'] else 'VETO'} — {t['reason']}")
        print(f"   blind:  {'PASS' if b['ok'] else 'VETO'} — {b['reason']}")
    else:
        print("   not reached")

    print("4. GUARD")
    guard = trace.get("guard", {})
    if guard.get("reached"):
        print(f"   {guard['decision']} — {guard['reason']} "
              f"({guard['approved_contracts']} contract(s) approved)")
    else:
        print("   not reached")

    print("5. EXECUTION")
    execution = trace.get("execution", {})
    if execution.get("reached") and execution.get("payload"):
        print(json.dumps(execution["payload"], indent=2))
    elif execution.get("reached"):
        print("   no payload — the guard refused this candidate")
    else:
        print("   not reached")

    print()
    print(f"REPLAY {'MATCHED' if result.matched else 'DIVERGED'} the recorded outcome")
    for m in result.mismatches:
        print(f"   - {m}")


# ── CLI ───────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--scenario", help="replay one named scenario")
    parser.add_argument("--all", action="store_true", help="replay every committed scenario")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument("--verify", action="store_true",
                        help="replay every scenario and assert the recomputed outcome "
                             "matches the recorded one, byte for byte")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--scenarios-dir", default=None,
                        help="override judge/scenarios/ (mainly for tests)")
    args = parser.parse_args(argv)

    scenarios_dir = Path(args.scenarios_dir) if args.scenarios_dir else None

    if args.list:
        names = list_scenarios(scenarios_dir)
        if args.json:
            print(json.dumps(names))
        else:
            for name in names:
                print(name)
        return 0

    if args.verify:
        results = verify_all(scenarios_dir)
        if args.json:
            print(json.dumps(
                {name: {"matched": ok, "mismatches": mismatches}
                 for name, (ok, mismatches) in results.items()},
                indent=2, sort_keys=True))
        else:
            for name, (ok, mismatches) in sorted(results.items()):
                print(f"{name:12s} {'OK' if ok else 'MISMATCH'}")
                for m in mismatches:
                    print(f"    - {m}")
        return 0 if all(ok for ok, _ in results.values()) else 1

    if args.all:
        names = list_scenarios(scenarios_dir)
        if not names:
            print("no scenarios found", file=sys.stderr)
            return 1
        all_ok = True
        if args.json:
            out = {}
            for name in names:
                try:
                    result = replay_scenario(name, scenarios_dir)
                    out[name] = {"matched": result.matched, "mismatches": result.mismatches}
                    all_ok = all_ok and result.matched
                except ScenarioError as e:
                    out[name] = {"matched": False, "mismatches": [str(e)]}
                    all_ok = False
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            for name in names:
                try:
                    result = replay_scenario(name, scenarios_dir)
                    _print_trace(result)
                    all_ok = all_ok and result.matched
                except ScenarioError as e:
                    print(f"scenario: {name}\n   ERROR: {e}")
                    all_ok = False
                print("-" * 60)
        return 0 if all_ok else 1

    if args.scenario:
        try:
            result = replay_scenario(args.scenario, scenarios_dir)
        except ScenarioError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(render_json(args.scenario, scenarios_dir))
        else:
            _print_trace(result)
        return 0 if result.matched else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
