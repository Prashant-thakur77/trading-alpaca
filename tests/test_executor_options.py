import os, sys
from datetime import date, timedelta
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from candidate_builder import OptionQuote, build_bull_put_spread
from risk_guard import RiskGuard, PortfolioState, load_risk_config
from journal import Journal
from executor_options import OptionsExecutor

EXPIRY = date.today() + timedelta(days=30)
RISK_YAML = os.path.join(os.path.dirname(__file__), "..", "risk.yaml")


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(f"SPY-{strike}{right}", "SPY", strike, EXPIRY, right, bid, ask, oi)


def _intent(contracts=1):
    return build_bull_put_spread(_q(445, "p", 3.00, 3.10), _q(440, "p", 2.00, 2.10),
                                 contracts=contracts)


def _flat():
    return PortfolioState(0, 0.0, 0.0, 0.0, 0, {})


class FakeCLI:
    def __init__(self, order=None, statuses=None, raises=False):
        self.posted, self.raises = [], raises
        self._order = order or {"id": "o-1", "status": "accepted"}
        self._statuses = list(statuses or ["filled"])

    def post_order(self, payload):
        if self.raises:
            raise RuntimeError("broker rejected: insufficient options level")
        self.posted.append(payload)
        return self._order

    def get_order(self, order_id):
        status = self._statuses.pop(0) if self._statuses else "filled"
        return {"id": order_id, "status": status, "filled_avg_price": "1.00"}


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL", raising=False)
    monkeypatch.chdir(tmp_path)
    guard = RiskGuard(load_risk_config(RISK_YAML))
    return lambda cli: OptionsExecutor(cli, guard, Journal(tmp_path / "j.jsonl"))


def test_submits_a_compliant_order(executor):
    cli = FakeCLI()
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "filled"
    assert len(cli.posted) == 1


def test_denied_order_is_never_sent(executor):
    """Guard DENY must stop the order reaching the broker at all."""
    cli = FakeCLI()
    state = PortfolioState(3, 0.0, 0.0, 0.0, 0, {})   # at max_positions
    result = executor(cli).submit(_intent(), state)
    assert result.status == "denied"
    assert cli.posted == []


def test_downsize_submits_the_approved_quantity(executor):
    """3 contracts risks $1200 > $1000 cap, so 2 must be sent, not 3."""
    cli = FakeCLI()
    result = executor(cli).submit(_intent(contracts=3), _flat())
    assert result.contracts == 2
    assert cli.posted[0]["qty"] == "2"


def test_broker_rejection_is_reported_not_raised(executor):
    cli = FakeCLI(raises=True)
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "rejected"
    assert "options level" in result.reason


def test_pending_order_is_reported_after_polling(executor):
    cli = FakeCLI(statuses=["new", "new", "new"])
    result = executor(cli).submit(_intent(), _flat(), poll_seconds=0)
    assert result.status == "pending"


def test_kill_switch_blocks_submission(executor, tmp_path):
    (tmp_path / "KILL_SWITCH").touch()
    cli = FakeCLI()
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "denied"
    assert cli.posted == []


def test_every_outcome_is_journalled(executor, tmp_path):
    cli = FakeCLI()
    ex = executor(cli)
    ex.submit(_intent(), _flat())
    types = [e["type"] for e in ex.journal.entries()]
    assert "verdict" in types
    assert "fill" in types


def test_denied_orders_are_journalled_too(executor, tmp_path):
    """A refusal is a decision and must be as auditable as a trade."""
    cli = FakeCLI()
    ex = executor(cli)
    ex.submit(_intent(), PortfolioState(3, 0.0, 0.0, 0.0, 0, {}))
    assert any(e["type"] == "verdict" for e in ex.journal.entries())
