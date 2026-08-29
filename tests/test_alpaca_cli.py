import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alpaca_cli import AlpacaCLI, AlpacaCLIError


class FakeCompleted:
    def __init__(self, returncode=0, stdout="{}", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _runner(result, calls):
    def run(args, **kwargs):
        calls.append({"args": args, "input": kwargs.get("input"), "env": kwargs.get("env")})
        return result
    return run


def test_get_account_parses_json():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"equity":"100000"}'), calls))
    assert cli.get_account() == {"equity": "100000"}


def test_account_command_requests_json_and_quiet():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="{}"), calls))
    cli.get_account()
    assert calls[0]["args"][:3] == ["alpaca", "account", "get"]
    assert "--quiet" in calls[0]["args"]


def test_forces_paper_mode_in_env():
    """Never let an inherited env var flip this to live trading."""
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="{}"), calls))
    cli.get_account()
    assert calls[0]["env"]["ALPACA_LIVE_TRADE"] == "false"


def test_post_order_sends_payload_as_stdin_json():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"id":"abc"}'), calls))
    payload = {"order_class": "mleg", "qty": "1"}
    assert cli.post_order(payload) == {"id": "abc"}
    assert calls[0]["args"][1:4] == ["api", "POST", "/v2/orders"]
    assert json.loads(calls[0]["input"]) == payload


def test_nonzero_exit_raises_with_stderr():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(returncode=1, stdout="", stderr="boom"), []))
    with pytest.raises(AlpacaCLIError, match="boom"):
        cli.get_account()


def test_unparseable_output_raises():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="not json"), []))
    with pytest.raises(AlpacaCLIError, match="parse"):
        cli.get_account()


def test_list_positions_returns_list():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='[{"symbol":"SPY"}]'), []))
    assert cli.list_positions() == [{"symbol": "SPY"}]


def test_empty_output_is_an_empty_list_for_positions():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout=""), []))
    assert cli.list_positions() == []


def test_non_list_positions_body_raises_rather_than_reading_as_flat():
    """"Account is flat" is load-bearing: it gates the fresh-account preflight
    and the position cap. An unexpected JSON shape is an anomaly, not proof
    that nothing is open, so it must fail closed rather than coerce to []."""
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"message":"forbidden"}'), []))
    with pytest.raises(AlpacaCLIError, match="list"):
        cli.list_positions()


def test_list_orders_queries_open_orders():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="[]"), calls))
    assert cli.list_orders() == []
    args = calls[0]["args"]
    assert args[:3] == ["alpaca", "order", "list"]
    assert "--status" in args and "open" in args
    assert "--nested" in args      # mleg legs must come back with the parent


def test_list_orders_returns_working_orders():
    body = '[{"id":"o-1","status":"new","order_class":"mleg","legs":[{"symbol":"SPY260918P00445000"}]}]'
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout=body), []))
    orders = cli.list_orders()
    assert len(orders) == 1
    assert orders[0]["id"] == "o-1"


def test_list_orders_filters_out_terminal_statuses():
    """Defence in depth: whatever the server filter does, a filled or canceled
    order is not a working order and must never block or count as one."""
    body = ('[{"id":"a","status":"filled"},{"id":"b","status":"canceled"},'
            '{"id":"c","status":"partially_filled"}]')
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout=body), []))
    assert [o["id"] for o in cli.list_orders()] == ["c"]


def test_list_orders_empty_output_is_no_orders():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout=""), []))
    assert cli.list_orders() == []


def test_list_orders_non_list_body_raises():
    """Fail closed: an unreadable order book must never look like 'none open',
    which is what lets a duplicate live order through."""
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"message":"nope"}'), []))
    with pytest.raises(AlpacaCLIError, match="list"):
        cli.list_orders()
