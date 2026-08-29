"""
Submit defined-risk multi-leg options orders through the Alpaca CLI.

Nothing reaches the broker without a RiskGuard verdict first, and every
outcome — including a refusal — is journalled. Broker rejections are returned
as results, never raised, so one bad order cannot kill a scan cycle.
"""
import logging
import time
from dataclasses import dataclass

from options_orders import build_mleg_payload

logger = logging.getLogger(__name__)


TERMINAL_FILLED = {"filled"}
TERMINAL_DEAD = {"canceled", "expired", "rejected", "done_for_day"}
# Not terminal — a partial can still complete — but it needs its own outcome:
# real spreads are working in the market and fewer filled than were requested.
PARTIAL = "partially_filled"


def _int_or_zero(value) -> int:
    """Broker quantities arrive as strings and may be absent. Never raise."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class ExecutionResult:
    status: str          # "filled" | "partially_filled" | "pending" | "rejected" | "denied"
    order_id: str = ""
    filled: bool = False
    reason: str = ""
    contracts: int = 0


class OptionsExecutor:
    def __init__(self, cli, guard, journal, clock=time.monotonic, sleep=time.sleep):
        self.cli, self.guard, self.journal = cli, guard, journal
        self._clock, self._sleep = clock, sleep

    def submit(self, intent, state, poll_seconds: int = 30,
               position_delta: float = 0.0, position_vega: float = 0.0) -> ExecutionResult:
        verdict = self.guard.evaluate(intent, state, position_delta, position_vega)
        self._record("verdict", {
            "underlying": getattr(intent, "underlying", "?"),
            "structure": getattr(intent, "structure", "?"),
            "decision": getattr(verdict.decision, "value", str(verdict.decision)),
            "reason": verdict.reason,
            "approved_contracts": verdict.approved_contracts,
        })

        # Fail closed by shape: require an affirmatively tradeable verdict
        # rather than "not DENY". If a verdict member is ever added, it denies
        # by default instead of trading by default.
        if not verdict.is_tradeable:
            logger.info("Order not tradeable (%s): %s", verdict.decision, verdict.reason)
            return ExecutionResult("denied", reason=verdict.reason)

        contracts = verdict.approved_contracts
        payload = build_mleg_payload(intent, contracts)
        self._record("proposal", {"payload": payload})

        try:
            order = self.cli.post_order(payload)
        except Exception as e:
            logger.error("Broker rejected the order: %s", e)
            self._record("rejected", {"error": str(e)})
            return ExecutionResult("rejected", reason=str(e), contracts=contracts)

        order_id = str(order.get("id", ""))
        underlying = getattr(intent, "underlying", "?")
        structure = getattr(intent, "structure", "?")
        last = self._poll(order_id, poll_seconds)
        status = str(last.get("status", "new"))

        if status in TERMINAL_FILLED:
            self._record("fill", {
                "order_id": order_id, "contracts": contracts,
                "underlying": underlying, "structure": structure,
            })
            return ExecutionResult("filled", order_id, True, "filled", contracts)

        if status in TERMINAL_DEAD:
            self._record("rejected", {
                "order_id": order_id, "status": status,
                "underlying": underlying, "structure": structure,
            })
            return ExecutionResult("rejected", order_id, reason=status, contracts=contracts)

        if status == PARTIAL:
            filled = _int_or_zero(last.get("filled_qty"))
            self._record("partial_fill", {
                "order_id": order_id, "filled_contracts": filled,
                "requested_contracts": contracts,
                "underlying": underlying, "structure": structure,
            })
            return ExecutionResult(
                PARTIAL, order_id, filled=filled > 0,
                reason=f"{filled} of {contracts} contract(s) filled; the remainder "
                       f"is still working at the broker",
                contracts=filled,
            )

        self._record("pending", {
            "order_id": order_id, "status": status, "contracts": contracts,
            "underlying": underlying, "structure": structure,
        })
        return ExecutionResult("pending", order_id, reason=status, contracts=contracts)

    def _record(self, entry_type: str, payload: dict) -> None:
        """Journal an outcome. A journal failure must never escape submit():
        once an order exists at the broker, an exception here would look to the
        caller like no order was placed, inviting a duplicate submission."""
        try:
            self.journal.append(entry_type, payload)
        except Exception as e:
            logger.error("Journal write failed for %s: %s", entry_type, e, exc_info=True)

    def _poll(self, order_id: str, poll_seconds: int) -> dict:
        """Poll until terminal or the window elapses. Returns the last order.

        The whole order is returned, not just its status, because a partial
        fill's `filled_qty` is what actually reached the market.
        """
        deadline = self._clock() + poll_seconds
        last: dict = {"status": "new"}
        while True:
            try:
                last = self.cli.get_order(order_id) or {}
            except Exception as e:
                logger.warning("Could not read order %s: %s", order_id, e)
                return last
            status = str(last.get("status", "new"))
            if status in TERMINAL_FILLED or status in TERMINAL_DEAD:
                return last
            if self._clock() >= deadline:
                return last
            self._sleep(1)
