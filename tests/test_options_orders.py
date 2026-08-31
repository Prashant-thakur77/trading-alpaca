import os, sys
from datetime import date, datetime, timedelta, timezone
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from candidate_builder import OptionQuote, build_bull_put_spread, build_long_straddle
from options_orders import build_mleg_payload, closing_payload

EXPIRY = date.today() + timedelta(days=30)


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
                       "SPY", strike, EXPIRY, right, bid, ask, oi)


def _credit_spread(contracts=1):
    return build_bull_put_spread(_q(445, "p", 3.00, 3.10), _q(440, "p", 2.00, 2.10),
                                 contracts=contracts)


def test_payload_uses_mleg_order_class():
    assert build_mleg_payload(_credit_spread(), 1)["order_class"] == "mleg"


def test_payload_has_no_top_level_symbol_or_side():
    """Alpaca rejects multi-leg orders that carry a top-level symbol or side."""
    payload = build_mleg_payload(_credit_spread(), 1)
    assert "symbol" not in payload
    assert "side" not in payload


def test_time_in_force_is_day():
    """Alpaca supports only day TIF for multi-leg options orders."""
    assert build_mleg_payload(_credit_spread(), 1)["time_in_force"] == "day"


def test_each_leg_carries_symbol_side_and_ratio():
    legs = build_mleg_payload(_credit_spread(), 1)["legs"]
    assert len(legs) == 2
    for leg in legs:
        assert leg["symbol"]
        assert leg["side"] in ("buy", "sell")
        assert leg["ratio_qty"] == "1"
        assert leg["position_intent"] in ("buy_to_open", "sell_to_open")


def test_credit_spread_uses_a_negative_limit_price():
    """Alpaca's convention: negative limit price = net credit received."""
    payload = build_mleg_payload(_credit_spread(), 1)
    assert float(payload["limit_price"]) < 0


def test_debit_structure_uses_a_positive_limit_price():
    straddle = build_long_straddle(_q(450, "c", 5.00, 5.10), _q(450, "p", 4.00, 4.10))
    assert float(build_mleg_payload(straddle, 1)["limit_price"]) > 0


def test_qty_is_the_strategy_multiplier():
    assert build_mleg_payload(_credit_spread(contracts=3), 3)["qty"] == "3"


def test_explicit_limit_price_overrides_the_computed_one():
    payload = build_mleg_payload(_credit_spread(), 1, limit_price=-0.95)
    assert float(payload["limit_price"]) == pytest.approx(-0.95)


def test_rejects_zero_contracts():
    with pytest.raises(ValueError, match="contracts"):
        build_mleg_payload(_credit_spread(), 0)


def test_rejects_more_than_four_legs():
    from dataclasses import replace
    intent = _credit_spread()
    too_many = replace(intent, legs=intent.legs * 3)   # 6 legs
    with pytest.raises(ValueError, match="4 legs"):
        build_mleg_payload(too_many, 1)


def test_rejects_duplicate_leg_symbols():
    from dataclasses import replace
    intent = _credit_spread()
    dupe = replace(intent, legs=(intent.legs[0], intent.legs[0]))
    with pytest.raises(ValueError, match="unique"):
        build_mleg_payload(dupe, 1)


def test_closing_payload_inverts_every_side():
    opening = build_mleg_payload(_credit_spread(), 1)
    closing = closing_payload(_credit_spread(), 1, limit_price=0.50)
    for o, c in zip(opening["legs"], closing["legs"]):
        assert o["side"] != c["side"]


def test_closing_payload_uses_close_position_intents():
    closing = closing_payload(_credit_spread(), 1, limit_price=0.50)
    for leg in closing["legs"]:
        assert leg["position_intent"] in ("buy_to_close", "sell_to_close")


class TestClientOrderId:
    """Broker-side idempotency (C1).

    The session can legitimately be re-run — a cron tick, an operator retry, a
    crash after POST. Without a deterministic client_order_id the second run
    submits a second identical spread and both can fill. Alpaca rejects a
    duplicate client_order_id, which turns "submit twice" into "one order plus
    a loud rejection".
    """

    def test_payload_carries_a_client_order_id(self):
        assert build_mleg_payload(_credit_spread(), 1)["client_order_id"]

    def test_same_trade_on_the_same_day_yields_the_same_id(self):
        a = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        b = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        assert a == b

    def test_downsized_retry_of_the_same_trade_keeps_the_same_id(self):
        """A guard downsize does not make it a different trade; re-submitting
        1 contract of a spread already working must still be rejected."""
        assert (build_mleg_payload(_credit_spread(contracts=3), 3)["client_order_id"]
                == build_mleg_payload(_credit_spread(contracts=3), 1)["client_order_id"])

    def test_a_different_structure_yields_a_different_id(self):
        straddle = build_long_straddle(_q(450, "c", 5.00, 5.10), _q(450, "p", 4.00, 4.10))
        assert (build_mleg_payload(straddle, 1)["client_order_id"]
                != build_mleg_payload(_credit_spread(), 1)["client_order_id"])

    def test_different_strikes_yield_a_different_id(self):
        other = build_bull_put_spread(_q(435, "p", 3.00, 3.10), _q(430, "p", 2.00, 2.10))
        assert (build_mleg_payload(other, 1)["client_order_id"]
                != build_mleg_payload(_credit_spread(), 1)["client_order_id"])

    def test_id_is_broker_legal_length(self):
        """Alpaca accepts up to 128 characters."""
        cid = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        assert 0 < len(cid) <= 128
        assert cid.isalnum()

    def test_id_changes_on_a_new_trading_day(self):
        """Yesterday's identical spread must not block today's."""
        import options_orders
        from datetime import date as real_date
        today = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        tomorrow = options_orders.client_order_id(
            _credit_spread(), on=real_date.today() + timedelta(days=1))
        assert today != tomorrow

    def test_default_id_tracks_utc_date_not_local_date(self, monkeypatch):
        """Load-bearing: the live session runs 19:00-01:30 IST, straddling
        LOCAL midnight, while the per-day submission cap in
        scripts/run_session.py is keyed on the UTC date. If the default id
        followed the local calendar date, the broker-side idempotency key
        would silently change mid-session while the UTC day has not —
        exactly the window where duplicate-order protection matters most.

        Freeze datetime.now(utc) to one instant and let the local date
        "advance" across the two calls: with the fix, the id must not move,
        because it must never consult the local date at all.
        """
        import options_orders as oo

        fixed_utc = datetime(2026, 8, 29, 19, 0, tzinfo=timezone.utc)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc

        local_dates = iter([date(2026, 8, 29), date(2026, 8, 30)])

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return next(local_dates)

        monkeypatch.setattr(oo, "datetime", FrozenDateTime, raising=False)
        monkeypatch.setattr(oo, "date", FrozenDate)

        a = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        b = build_mleg_payload(_credit_spread(), 1)["client_order_id"]
        assert a == b


# ── marketable concession ─────────────────────────────────────

def test_zero_concession_prices_at_the_theoretical_mid():
    """The historical behaviour, kept reachable and pinned.

    _credit_spread() sells the 445p (mid 3.05) and buys the 440p (mid 2.05),
    so the theoretical net credit is $1.00 per share.
    """
    p = build_mleg_payload(_credit_spread(), 1)
    assert p["limit_price"] == "-1.00", "default must stay at the theoretical mid"


def test_credit_order_concedes_toward_the_market():
    """Regression for 2026-08-31 20:34.

    A live order priced at the theoretical mid credit ($1.96) rested 55
    minutes unfilled against a spread the market was paying $1.63 for, and
    blocked every later cycle through the no-stacking guard. A mid-priced
    limit on a two-leg spread only fills if someone crosses to us. Conceding
    shrinks the credit we demand, which is what makes it marketable.
    """
    p = build_mleg_payload(_credit_spread(), 1, concession=0.20)
    # 1.00 credit, conceding 20% -> ask only 0.80
    assert p["limit_price"] == "-0.80"


def test_concession_never_flips_a_credit_into_a_debit():
    """Conceding everything must still not pay to open a credit spread."""
    p = build_mleg_payload(_credit_spread(), 1, concession=1.5)
    assert float(p["limit_price"]) <= 0.0


def test_negative_concession_is_refused():
    with pytest.raises(ValueError):
        build_mleg_payload(_credit_spread(), 1, concession=-0.1)
