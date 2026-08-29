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
from risk_guard import Verdict

logger = logging.getLogger(__name__)

TERMINAL_FILLED = {"filled"}
TERMINAL_DEAD = {"canceled", "expired", "rejected", "done_for_day"}


@dataclass(frozen=True)
class ExecutionResult:
    status: str          # "filled" | "pending" | "rejected" | "denied"
    order_id: str = ""
    filled: bool = False
    reason: str = ""
    contracts: int = 0


class OptionsExecutor:
    def __init__(self, cli, guard, journal, clock=time.monotonic, sleep=time.sleep):
        self.cli, self.guard, self.journal = cli, guard, journal
        self._clock, self._sleep = clock, sleep

    def submit(self, intent, state, poll_seconds: int = 30) -> ExecutionResult:
        verdict = self.guard.evaluate(intent, state)
        self._record("verdict", {
            "underlying": getattr(intent, "underlying", "?"),
            "structure": getattr(intent, "structure", "?"),
            "decision": verdict.decision.value,
            "reason": verdict.reason,
            "approved_contracts": verdict.approved_contracts,
        })

        if verdict.decision == Verdict.DENY:
            logger.info("Order denied by guard: %s", verdict.reason)
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
        status = self._poll(order_id, poll_seconds)

        if status in TERMINAL_FILLED:
            self._record("fill", {"order_id": order_id, "contracts": contracts})
            return ExecutionResult("filled", order_id, True, "filled", contracts)

        if status in TERMINAL_DEAD:
            self._record("rejected", {"order_id": order_id, "status": status})
            return ExecutionResult("rejected", order_id, reason=status, contracts=contracts)

        self._record("pending", {"order_id": order_id, "status": status})
        return ExecutionResult("pending", order_id, reason=status, contracts=contracts)

    def _record(self, entry_type: str, payload: dict) -> None:
        """Journal an outcome. A journal failure must never escape submit():
        once an order exists at the broker, an exception here would look to the
        caller like no order was placed, inviting a duplicate submission."""
        try:
            self.journal.append(entry_type, payload)
        except Exception as e:
            logger.error("Journal write failed for %s: %s", entry_type, e, exc_info=True)

    def _poll(self, order_id: str, poll_seconds: int) -> str:
        """Poll until terminal or the window elapses. Returns the last status."""
        deadline = self._clock() + poll_seconds
        status = "new"
        while True:
            try:
                status = str(self.cli.get_order(order_id).get("status", "new"))
            except Exception as e:
                logger.warning("Could not read order %s: %s", order_id, e)
                return status
            if status in TERMINAL_FILLED or status in TERMINAL_DEAD:
                return status
            if self._clock() >= deadline:
                return status
            self._sleep(1)
