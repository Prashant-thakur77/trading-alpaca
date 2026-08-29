"""
Subprocess adapter over the official Alpaca CLI.

This is the hackathon compliance path: the rules require the Trading API plus
the MCP server or the CLI, and orders are routed through the CLI binary here.
alpaca-py is retained for analysis, where the CLI is awkward.

Paper mode is forced in the environment on every call, so an inherited
ALPACA_LIVE_TRADE cannot flip this to live trading.
"""
import json
import logging
import os
import subprocess
from shutil import which

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# An order in one of these states can never fill again, so it is not "working".
# Everything else — new, accepted, pending_new, held, partially_filled, ... —
# can still trade and therefore blocks a second submission of the same idea.
TERMINAL_ORDER_STATUSES = frozenset({
    "filled", "canceled", "expired", "rejected", "done_for_day", "replaced",
})


class AlpacaCLIError(RuntimeError):
    """The CLI exited non-zero, timed out, or emitted unparseable output."""


class AlpacaCLI:
    def __init__(self, binary: str = "alpaca", runner=subprocess.run,
                 timeout: int = DEFAULT_TIMEOUT):
        self.binary, self._runner, self.timeout = binary, runner, timeout

    def available(self) -> bool:
        return which(self.binary) is not None

    def _env(self) -> dict:
        env = dict(os.environ)
        env["ALPACA_LIVE_TRADE"] = "false"   # paper only, ever
        env["ALPACA_OUTPUT"] = "json"
        return env

    def run(self, args: list[str], payload: dict | None = None) -> dict | list:
        cmd = [self.binary, *args]
        try:
            result = self._runner(
                cmd, capture_output=True, text=True, timeout=self.timeout,
                env=self._env(),
                input=json.dumps(payload) if payload is not None else None,
            )
        except subprocess.TimeoutExpired as e:
            raise AlpacaCLIError(f"CLI timed out after {self.timeout}s: {' '.join(cmd)}") from e

        if result.returncode != 0:
            raise AlpacaCLIError(
                f"CLI failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
            )

        out = (result.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise AlpacaCLIError(f"Could not parse CLI output as JSON: {out[:200]}") from e

    def get_account(self) -> dict:
        return self.run(["account", "get", "--quiet"])

    def _as_list(self, out, what: str) -> list:
        """Narrow a CLI body to a list, or fail closed.

        An empty stdout is a genuine "none" (the CLI prints nothing). Any other
        non-list body is an anomaly — an error envelope, a schema change — and
        must NOT be read as "nothing is open": that signal gates the
        fresh-account preflight, the position cap and the duplicate-order check.
        """
        if out == {}:            # empty stdout, normalized by run()
            return []
        if not isinstance(out, list):
            raise AlpacaCLIError(
                f"Expected a list of {what} from the CLI, got {type(out).__name__}: "
                f"{str(out)[:200]}"
            )
        return out

    def list_positions(self) -> list:
        return self._as_list(self.run(["position", "list", "--quiet"]), "positions")

    def list_orders(self, status: str = "open", limit: int = 500) -> list:
        """Working (not yet terminal) orders, with multi-leg legs nested.

        `position list` does not show an order that has not filled. Without
        this, a limit order left working all day is invisible to the next
        session, which then submits the same spread a second time.
        """
        out = self.run([
            "order", "list",
            "--status", status,
            "--nested",                 # mleg legs roll up under the parent
            "--limit", str(limit),
            "--quiet",
        ])
        orders = self._as_list(out, "orders")
        return [o for o in orders
                if str(o.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES]

    def get_order(self, order_id: str) -> dict:
        return self.run(["order", "get", "--order-id", order_id, "--quiet"])

    def post_order(self, payload: dict) -> dict:
        return self.run(["api", "POST", "/v2/orders"], payload=payload)
