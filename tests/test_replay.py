"""Tests for scripts/replay.py — the credential-free judge replay engine.

The whole point of the judge surface (design spec 4.6) is that a judge can
replay a real decision with NO credentials, NO network and NO LLM call, and
verify the recomputed guard verdict and order payload match what was
recorded, byte for byte. These tests exercise exactly that property against
the four committed fixtures in judge/scenarios/.
"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import replay  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCENARIOS_DIR = os.path.join(REPO_ROOT, "judge", "scenarios")
ALL_SCENARIOS = ("allow", "deny", "downsize", "fail_closed")
TRADE_SCENARIOS = ("allow", "deny", "downsize")

# The calendar date the four committed fixtures were recorded on. Every
# expiry, OCC symbol and DTE in them is measured from THIS date, not from
# whatever day a judge happens to open the page.
FIXTURE_AS_OF = date(2026, 8, 29)


def _freeze_clock(monkeypatch, when):
    """Make `date.today()` return `when` everywhere the replay path could
    consult it, without touching the real system clock.

    `candidate_builder.OptionQuote.dte` is the only live `date.today()` call
    reachable from a replay, and `scripts/replay.py` used to add a second one
    of its own. Freezing both is what lets a single test assert the property
    that actually matters: a replay's output must not be a function of the
    day it is run on.
    """
    import candidate_builder

    frozen = type("_FrozenDate", (date,), {
        "today": classmethod(lambda cls: when),
    })
    for module in (candidate_builder, replay):
        if isinstance(getattr(module, "date", None), type):
            monkeypatch.setattr(module, "date", frozen)
    return when


class TestListScenarios:
    def test_lists_all_four_committed_scenarios(self):
        assert sorted(replay.list_scenarios()) == sorted(ALL_SCENARIOS)


class TestLoadFixture:
    def test_missing_scenario_raises(self):
        with pytest.raises(replay.ScenarioError):
            replay.load_fixture("does-not-exist")

    def test_corrupted_json_is_detected_not_silently_rendered(self, tmp_path, monkeypatch):
        """A corrupted fixture must fail loudly, never render wrong data."""
        bad_dir = tmp_path / "scenarios"
        bad_dir.mkdir()
        (bad_dir / "allow.json").write_text("{not valid json,,,")
        monkeypatch.setattr(replay, "SCENARIOS_DIR", bad_dir)
        with pytest.raises(replay.ScenarioError):
            replay.load_fixture("allow")

    def test_scenario_name_mismatch_is_detected(self, tmp_path, monkeypatch):
        bad_dir = tmp_path / "scenarios"
        bad_dir.mkdir()
        (bad_dir / "allow.json").write_text(json.dumps({"scenario": "deny"}))
        monkeypatch.setattr(replay, "SCENARIOS_DIR", bad_dir)
        with pytest.raises(replay.ScenarioError):
            replay.load_fixture("allow")

    def test_missing_required_field_is_detected(self, tmp_path, monkeypatch):
        bad_dir = tmp_path / "scenarios"
        bad_dir.mkdir()
        (bad_dir / "allow.json").write_text(json.dumps({"scenario": "allow"}))
        monkeypatch.setattr(replay, "SCENARIOS_DIR", bad_dir)
        with pytest.raises(replay.ScenarioError):
            replay.replay_scenario("allow")


class TestReplayEachScenario:
    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_replays_and_matches_recorded_outcome(self, name):
        result = replay.replay_scenario(name)
        assert result.matched, result.mismatches

    def test_allow_reaches_execution_with_a_payload(self):
        result = replay.replay_scenario("allow")
        assert result.trace["guard"]["decision"] == "ALLOW"
        assert result.trace["execution"]["payload"] is not None
        assert result.trace["execution"]["payload"]["order_class"] == "mleg"

    def test_deny_produces_no_payload(self):
        result = replay.replay_scenario("deny")
        assert result.trace["guard"]["decision"] == "DENY"
        assert result.trace["execution"]["payload"] is None

    def test_downsize_approves_fewer_contracts_than_requested(self):
        result = replay.replay_scenario("downsize")
        guard = result.trace["guard"]
        assert guard["decision"] == "ALLOW_WITH_DOWNSIZE"
        assert guard["approved_contracts"] < result.fixture["requested_contracts"]
        assert result.trace["execution"]["payload"]["qty"] == str(guard["approved_contracts"])

    def test_fail_closed_never_reaches_the_guard(self):
        result = replay.replay_scenario("fail_closed")
        assert result.trace["snapshot"]["ok"] is False
        for stage in ("committee", "veto", "guard", "execution"):
            assert result.trace[stage]["reached"] is False
        assert result.trace["snapshot"]["exit_code"] != 0


class TestVerifyAll:
    def test_verify_all_scenarios_pass(self):
        results = replay.verify_all()
        assert set(results) == set(ALL_SCENARIOS)
        for name, (ok, mismatches) in results.items():
            assert ok, f"{name}: {mismatches}"


class TestClockIndependence:
    """The judge page's whole claim is that these four decisions replay and
    reproduce their recorded verdicts, for anyone, forever. That claim is a
    claim about every future calendar day, so it gets tested on more than one.

    Regression: fixtures stored only a RELATIVE `dte`, and `rebuild_intent`
    anchored `expiry = date.today() + dte`. The reconstructed expiry therefore
    advanced one day per calendar day, the recomputed OCC symbols stopped
    matching the recorded ones (e.g. SPY261001C00777000 vs the recorded
    SPY260930C00777000), and every trade scenario reported MISMATCH from the
    day after the fixtures were generated onward.
    """

    OFFSETS = (0, 3, 30)

    @pytest.mark.parametrize("offset", OFFSETS)
    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_replay_matches_at_a_future_clock(self, name, offset, monkeypatch):
        when = _freeze_clock(monkeypatch, FIXTURE_AS_OF + timedelta(days=offset))
        result = replay.replay_scenario(name)
        assert result.matched, f"{name} at {when}: {result.mismatches}"

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_verify_all_passes_at_a_future_clock(self, offset, monkeypatch):
        _freeze_clock(monkeypatch, FIXTURE_AS_OF + timedelta(days=offset))
        results = replay.verify_all()
        assert set(results) == set(ALL_SCENARIOS)
        for name, (ok, mismatches) in results.items():
            assert ok, f"{name}: {mismatches}"

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_rendered_json_is_identical_at_every_clock(self, offset, monkeypatch):
        """Not merely 'still matches' — byte-identical output. A replay whose
        trace changed with the calendar would still be a drifting artifact."""
        baseline = {name: replay.render_json(name) for name in ALL_SCENARIOS}
        _freeze_clock(monkeypatch, FIXTURE_AS_OF + timedelta(days=offset))
        for name in ALL_SCENARIOS:
            assert replay.render_json(name) == baseline[name], name

    @pytest.mark.parametrize("offset", OFFSETS)
    @pytest.mark.parametrize("name", ("allow", "downsize"))
    def test_payload_symbols_are_the_recorded_ones_at_any_clock(
            self, name, offset, monkeypatch):
        _freeze_clock(monkeypatch, FIXTURE_AS_OF + timedelta(days=offset))
        result = replay.replay_scenario(name)
        recorded = result.fixture["recorded_outcome"]["payload"]["legs"]
        recomputed = result.trace["execution"]["payload"]["legs"]
        assert [leg["symbol"] for leg in recomputed] == \
               [leg["symbol"] for leg in recorded]

    @pytest.mark.parametrize("offset", OFFSETS)
    @pytest.mark.parametrize("name", TRADE_SCENARIOS)
    def test_reconstructed_dte_is_the_recorded_dte_at_any_clock(
            self, name, offset, monkeypatch):
        """The old anchoring got this right by construction and everything
        else wrong; the fix must keep it right for the right reason."""
        _freeze_clock(monkeypatch, FIXTURE_AS_OF + timedelta(days=offset))
        fixture = replay.load_fixture(name)
        chosen = next(c for c in fixture["candidates"]
                      if c["id"] == fixture["chosen_id"])
        intent = replay.rebuild_intent(
            chosen, fixture["market"]["underlying"],
            int(fixture["requested_contracts"]), replay.fixture_as_of(name, fixture))
        assert intent.dte == int(chosen["dte"])


class TestFixtureDateSchema:
    """The fixtures are a judged artifact: their dates must be absolute and
    self-consistent, not reconstructed from whatever day it is."""

    @pytest.mark.parametrize("name", TRADE_SCENARIOS)
    def test_fixture_declares_an_absolute_as_of(self, name):
        fixture = replay.load_fixture(name)
        assert fixture["as_of"] == FIXTURE_AS_OF.isoformat()
        assert replay.fixture_as_of(name, fixture) == FIXTURE_AS_OF

    @pytest.mark.parametrize("name", TRADE_SCENARIOS)
    def test_every_candidate_carries_an_absolute_expiry(self, name):
        fixture = replay.load_fixture(name)
        for cand in fixture["candidates"]:
            expiry = date.fromisoformat(cand["expiry"])
            assert (expiry - FIXTURE_AS_OF).days == int(cand["dte"]), cand["id"]

    @pytest.mark.parametrize("name", TRADE_SCENARIOS)
    def test_chosen_candidate_expiry_matches_the_recorded_occ_symbols(self, name):
        """The expiry added to each fixture is not a new fact — it is the one
        already encoded in the recorded order payload's OCC symbols."""
        fixture = replay.load_fixture(name)
        chosen = next(c for c in fixture["candidates"]
                      if c["id"] == fixture["chosen_id"])
        stamp = date.fromisoformat(chosen["expiry"]).strftime("%y%m%d")
        for leg in fixture["recorded_outcome"]["chosen_intent"]["legs"]:
            assert leg["symbol"][3:9] == stamp, leg["symbol"]

    def test_a_candidate_whose_dte_contradicts_its_expiry_is_rejected(self, tmp_path):
        """`dte` is kept for readability but never trusted: a fixture whose
        two representations disagree is corrupt and must fail loudly."""
        with pytest.raises(replay.ScenarioError):
            replay.rebuild_intent(
                {"id": "c1", "structure": "bear_call_spread", "dte": 99,
                 "expiry": "2026-09-30",
                 "legs": [
                     {"side": "sell", "strike": 777.0, "right": "c",
                      "open_interest": 851, "bid": 7.04, "ask": 7.05},
                     {"side": "buy", "strike": 782.0, "right": "c",
                      "open_interest": 669, "bid": 4.87, "ask": 4.99},
                 ]},
                "SPY", 1, FIXTURE_AS_OF)

    def test_a_candidate_with_no_expiry_is_rejected_not_guessed(self):
        """The bug was a guess (`today + dte`). Refusing is the fix; falling
        back to any clock-derived expiry would reintroduce the drift."""
        with pytest.raises(replay.ScenarioError):
            replay.rebuild_intent(
                {"id": "c1", "structure": "bear_call_spread", "dte": 32,
                 "legs": [
                     {"side": "sell", "strike": 777.0, "right": "c",
                      "open_interest": 851, "bid": 7.04, "ask": 7.05},
                     {"side": "buy", "strike": 782.0, "right": "c",
                      "open_interest": 669, "bid": 4.87, "ask": 4.99},
                 ]},
                "SPY", 1, FIXTURE_AS_OF)

    def test_a_trade_fixture_with_no_as_of_is_rejected(self, tmp_path, monkeypatch):
        bad_dir = tmp_path / "scenarios"
        bad_dir.mkdir()
        fixture = json.loads(
            open(os.path.join(SCENARIOS_DIR, "allow.json")).read())
        fixture.pop("as_of", None)
        fixture.pop("order_date", None)
        (bad_dir / "allow.json").write_text(json.dumps(fixture))
        with pytest.raises(replay.ScenarioError):
            replay.replay_scenario("allow", bad_dir)

    @pytest.mark.parametrize("name", TRADE_SCENARIOS)
    def test_quotes_carry_the_observation_date(self, name):
        """`OptionQuote.as_of` is what pins every DTE-dependent gate to the
        day the chain was observed instead of to the day of the replay."""
        fixture = replay.load_fixture(name)
        chosen = next(c for c in fixture["candidates"]
                      if c["id"] == fixture["chosen_id"])
        intent = replay.rebuild_intent(
            chosen, fixture["market"]["underlying"],
            int(fixture["requested_contracts"]), FIXTURE_AS_OF)
        for leg in intent.legs:
            assert leg.quote.as_of == FIXTURE_AS_OF


class TestDeterminism:
    def test_replaying_twice_is_byte_identical(self):
        first = replay.render_json("allow")
        second = replay.render_json("allow")
        assert first == second

    def test_replaying_twice_is_byte_identical_across_all_scenarios(self):
        for name in ALL_SCENARIOS:
            assert replay.render_json(name) == replay.render_json(name)


class TestNoNetworkNoLLM:
    def test_replay_never_calls_the_llm_client(self, monkeypatch):
        """Inject a client that raises if called — replay must never call it.

        `llm.client.call_claude` is the desk's only LLM/subprocess entry
        point (`committee/veto.py`'s `blind_review` and every committee role
        default to it). If replay ever fell back to a live call instead of
        reading the fixture's recorded response, this would fire.
        """
        def _boom(*a, **k):
            raise AssertionError("replay must never call the LLM client")

        monkeypatch.setattr("llm.client.call_claude", _boom)
        monkeypatch.setattr("committee.veto.call_claude", _boom)
        monkeypatch.setattr(subprocess, "run", _boom)

        for name in ALL_SCENARIOS:
            result = replay.replay_scenario(name)
            assert result.matched, result.mismatches

    def test_replay_works_with_no_alpaca_env_vars_set(self, monkeypatch):
        """Zero credentials: unset every ALPACA_* var and confirm replay still
        works — the judge path must never depend on them being present."""
        for key in list(os.environ):
            if key.startswith("ALPACA"):
                monkeypatch.delenv(key, raising=False)
        for name in ALL_SCENARIOS:
            result = replay.replay_scenario(name)
            assert result.matched, result.mismatches


class TestCLI:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "replay.py"), *args],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    def test_list_exits_zero_and_names_all_scenarios(self):
        proc = self._run("--list")
        assert proc.returncode == 0
        for name in ALL_SCENARIOS:
            assert name in proc.stdout

    def test_scenario_allow_exits_zero(self):
        proc = self._run("--scenario", "allow")
        assert proc.returncode == 0

    def test_scenario_fail_closed_exits_zero_because_the_refusal_is_the_recorded_outcome(self):
        """fail_closed's RECORDED outcome is itself a refusal (exit 1 from
        run_session.main). replay's own exit code reports whether the replay
        reproduced that recorded outcome — 0, because it does."""
        proc = self._run("--scenario", "fail_closed")
        assert proc.returncode == 0

    def test_unknown_scenario_exits_nonzero(self):
        proc = self._run("--scenario", "not-a-real-scenario")
        assert proc.returncode != 0

    def test_all_exits_zero(self):
        proc = self._run("--all")
        assert proc.returncode == 0
        for name in ALL_SCENARIOS:
            assert name in proc.stdout

    def test_all_json_is_valid_json(self):
        proc = self._run("--all", "--json")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert set(payload) == set(ALL_SCENARIOS)

    def test_verify_flag_exits_zero(self):
        proc = self._run("--verify")
        assert proc.returncode == 0

    def test_corrupted_fixture_exits_nonzero_via_cli(self, tmp_path, monkeypatch):
        bad_dir = tmp_path / "scenarios"
        bad_dir.mkdir()
        (bad_dir / "allow.json").write_text("{not valid json,,,")
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "replay.py"),
             "--scenario", "allow", "--scenarios-dir", str(bad_dir)],
            capture_output=True, text=True, cwd=REPO_ROOT, env=env,
        )
        assert proc.returncode != 0


class TestMakefileTarget:
    def test_judge_target_runs_replay_all(self):
        makefile = open(os.path.join(REPO_ROOT, "Makefile")).read()
        assert "judge:" in makefile
        assert "scripts/replay.py --all" in makefile
