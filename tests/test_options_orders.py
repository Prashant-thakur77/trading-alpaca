import os, sys
from datetime import date, timedelta
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
