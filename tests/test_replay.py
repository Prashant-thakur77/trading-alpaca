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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import replay  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCENARIOS_DIR = os.path.join(REPO_ROOT, "judge", "scenarios")
ALL_SCENARIOS = ("allow", "deny", "downsize", "fail_closed")


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
