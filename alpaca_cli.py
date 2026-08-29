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

    def list_positions(self) -> list:
        out = self.run(["position", "list", "--quiet"])
        return out if isinstance(out, list) else []

    def get_order(self, order_id: str) -> dict:
        return self.run(["order", "get", "--order-id", order_id, "--quiet"])

    def post_order(self, payload: dict) -> dict:
        return self.run(["api", "POST", "/v2/orders"], payload=payload)
