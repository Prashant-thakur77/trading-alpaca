"""
RiskGuard — the deterministic gate every order must pass.

Returns ALLOW / DENY / ALLOW_WITH_DOWNSIZE for a TradeIntent against the limits
in risk.yaml (CLAUDE.md hard rule 2). Nothing reaches a broker without a verdict
from here, and the guard is fail-closed by construction: a missing intent,
missing portfolio state, unreadable config or *any* internal exception all
produce DENY. There is no code path where an error results in a trade.

This complements risk_manager.py, which tracks running P&L/streak state across
the session. The guard is stateless — it judges one candidate against one
snapshot — which is what makes it exhaustively testable.
"""
import logging
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent / "risk.yaml"


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ALLOW_WITH_DOWNSIZE = "ALLOW_WITH_DOWNSIZE"


@dataclass(frozen=True)
class RiskDecision:
    """A guard verdict. `approved_contracts` is what may actually be sent."""
    decision: Verdict
    reason: str
    approved_contracts: int = 0

    @property
    def is_tradeable(self) -> bool:
        return self.decision in (Verdict.ALLOW, Verdict.ALLOW_WITH_DOWNSIZE)


@dataclass(frozen=True)
class PortfolioState:
    """Snapshot of the book the candidate would join."""
    open_positions: int
    net_delta: float
    net_vega: float
    daily_realized_pnl: float
    consecutive_losses: int
    new_today_by_underlying: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskConfig:
    """Limits loaded from risk.yaml. No defaults — the file is the truth."""
    paper_only: bool
    initial_capital: float
    min_options_level: int
    max_loss_per_position: float
    max_positions: int
    max_new_per_underlying_per_day: int
    max_abs_net_delta: float
    max_abs_net_vega: float
    min_dte: int
    max_dte: int
    max_spread_pct_of_mid: float
    min_open_interest: int
    max_daily_loss_pct: float
    consecutive_losses_to_halve: int
    size_scale_after_losses: float
    allowed_structures: tuple[str, ...]
    kill_switch_file: str
    kill_switch_env: str

    @property
    def max_daily_loss_dollars(self) -> float:
        return self.initial_capital * self.max_daily_loss_pct / 100.0


def load_risk_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RiskConfig:
    """Load risk.yaml. Raises if absent or malformed — never silently defaults.

    A guard running on invented limits is more dangerous than no guard, so a
    broken config must stop the process rather than degrade quietly.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"risk.yaml not found at {path} — refusing to run unguarded")

    data = yaml.safe_load(path.read_text())
    if not data:
        raise ValueError(f"risk.yaml at {path} is empty")

    try:
        return RiskConfig(
            paper_only=data["account"]["paper_only"],
            initial_capital=float(data["account"]["initial_capital"]),
            min_options_level=int(data["account"]["min_options_level"]),
            max_loss_per_position=float(data["position"]["max_loss_per_position"]),
            max_positions=int(data["position"]["max_positions"]),
            max_new_per_underlying_per_day=int(data["position"]["max_new_per_underlying_per_day"]),
            max_abs_net_delta=float(data["portfolio"]["max_abs_net_delta"]),
            max_abs_net_vega=float(data["portfolio"]["max_abs_net_vega"]),
            min_dte=int(data["contract"]["min_dte"]),
            max_dte=int(data["contract"]["max_dte"]),
            max_spread_pct_of_mid=float(data["contract"]["max_spread_pct_of_mid"]),
            min_open_interest=int(data["contract"]["min_open_interest"]),
            max_daily_loss_pct=float(data["daily"]["max_daily_loss_pct"]),
            consecutive_losses_to_halve=int(data["daily"]["consecutive_losses_to_halve"]),
            size_scale_after_losses=float(data["daily"]["size_scale_after_losses"]),
            allowed_structures=tuple(data["structures"]["allowed"]),
            kill_switch_file=str(data["kill_switch"]["file"]),
            kill_switch_env=str(data["kill_switch"]["env"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"risk.yaml at {path} is malformed: {e}") from e


class RiskGuard:
    """Stateless evaluator of one candidate against one portfolio snapshot."""

    def __init__(self, config: RiskConfig):
        self.config = config

    # ── kill switch ──────────────────────────────────────────
    def kill_switch_active(self) -> tuple[bool, str]:
        """Hard rule 6: file present or env var set to 1 halts all trading."""
        if Path(self.config.kill_switch_file).exists():
            return True, f"KILL SWITCH: {self.config.kill_switch_file} file present"
        if os.environ.get(self.config.kill_switch_env, "0") == "1":
            return True, f"KILL SWITCH: {self.config.kill_switch_env}=1"
        return False, ""

    # ── startup ──────────────────────────────────────────────
    def startup_checks(
        self, is_paper: bool, options_level: int, open_positions: int
    ) -> tuple[bool, str]:
        """Preflight before any session. All must hold or the agent must not run."""
        killed, why = self.kill_switch_active()
        if killed:
            return False, why
        if self.config.paper_only and not is_paper:
            return False, "Account is not a paper account — paper only, ever (hard rule 3)"
        if options_level < self.config.min_options_level:
            return False, (
                f"Options level {options_level} below required "
                f"{self.config.min_options_level} for defined-risk spreads"
            )
        if open_positions > 0:
            return False, (
                f"Account has {open_positions} pre-existing position(s); "
                f"expected a flat account at startup"
            )
        return True, ""

    # ── per-order verdict ────────────────────────────────────
    def evaluate(
        self,
        intent,
        state: PortfolioState | None,
        position_delta: float = 0.0,
        position_vega: float = 0.0,
    ) -> RiskDecision:
        """Judge one candidate. Any problem — including a bug — yields DENY."""
        try:
            return self._evaluate(intent, state, position_delta, position_vega)
        except Exception as e:
            logger.error("RiskGuard error, denying by default: %s", e, exc_info=True)
            return RiskDecision(Verdict.DENY, f"Guard error (fail-closed): {e}")

    def _evaluate(self, intent, state, position_delta, position_vega) -> RiskDecision:
        cfg = self.config

        killed, why = self.kill_switch_active()
        if killed:
            return RiskDecision(Verdict.DENY, why)

        if intent is None:
            return RiskDecision(Verdict.DENY, "No trade intent supplied")
        if state is None:
            return RiskDecision(Verdict.DENY, "No portfolio state supplied")

        contracts = int(intent.contracts)
        if contracts <= 0:
            return RiskDecision(Verdict.DENY, f"Non-positive contract count ({contracts})")

        # Structure allowlist — naked/undefined risk can never pass.
        if intent.structure not in cfg.allowed_structures:
            return RiskDecision(
                Verdict.DENY,
                f"Structure '{intent.structure}' is not in the allowed structure list",
            )
        if not math.isfinite(intent.max_loss):
            return RiskDecision(Verdict.DENY, "Intent has undefined (infinite) max loss")
        if not intent.is_defined_risk:
            return RiskDecision(Verdict.DENY, "Intent is not defined-risk")

        # Daily loss halt.
        if state.daily_realized_pnl <= -cfg.max_daily_loss_dollars:
            return RiskDecision(
                Verdict.DENY,
                f"Daily loss limit reached "
                f"(${state.daily_realized_pnl:,.2f} <= -${cfg.max_daily_loss_dollars:,.2f})",
            )

        # Concurrent position cap.
        if state.open_positions >= cfg.max_positions:
            return RiskDecision(
                Verdict.DENY,
                f"At max_positions ({state.open_positions}/{cfg.max_positions})",
            )

        # One new trade per underlying per day.
        already = state.new_today_by_underlying.get(intent.underlying, 0)
        if already >= cfg.max_new_per_underlying_per_day:
            return RiskDecision(
                Verdict.DENY,
                f"Already opened {already} position(s) in underlying "
                f"{intent.underlying} today",
            )

        # Portfolio Greeks after this trade.
        projected_delta = state.net_delta + position_delta
        if abs(projected_delta) > cfg.max_abs_net_delta:
            return RiskDecision(
                Verdict.DENY,
                f"Net delta would reach {projected_delta:+.1f}, "
                f"beyond +/-{cfg.max_abs_net_delta:.0f}",
            )
        projected_vega = state.net_vega + position_vega
        if abs(projected_vega) > cfg.max_abs_net_vega:
            return RiskDecision(
                Verdict.DENY,
                f"Net vega would reach {projected_vega:+.1f}, "
                f"beyond +/-{cfg.max_abs_net_vega:.0f}",
            )

        # Sizing: start from the request, then apply every downsizing rule.
        per_contract_loss = intent.max_loss / contracts
        if per_contract_loss > cfg.max_loss_per_position:
            return RiskDecision(
                Verdict.DENY,
                f"A single contract risks ${per_contract_loss:,.2f}, above "
                f"max_loss_per_position ${cfg.max_loss_per_position:,.2f}",
            )

        approved = contracts
        reasons: list[str] = []

        # Losing-streak size reduction.
        if state.consecutive_losses >= cfg.consecutive_losses_to_halve:
            scaled = int(approved * cfg.size_scale_after_losses)
            if scaled < 1:
                return RiskDecision(
                    Verdict.DENY,
                    f"{state.consecutive_losses} consecutive losses scales "
                    f"{approved} contract(s) below one — abstaining",
                )
            if scaled < approved:
                reasons.append(
                    f"{state.consecutive_losses} consecutive losses "
                    f"({approved}->{scaled} contracts)"
                )
                approved = scaled

        # Max loss per position cap.
        affordable = int(cfg.max_loss_per_position // per_contract_loss)
        if affordable < approved:
            reasons.append(
                f"max_loss ${approved * per_contract_loss:,.2f} exceeds "
                f"${cfg.max_loss_per_position:,.2f} ({approved}->{affordable} contracts)"
            )
            approved = affordable

        if approved < 1:
            return RiskDecision(Verdict.DENY, "Downsizing left fewer than one contract")

        if approved < contracts:
            return RiskDecision(
                Verdict.ALLOW_WITH_DOWNSIZE, "; ".join(reasons), approved_contracts=approved
            )

        return RiskDecision(
            Verdict.ALLOW,
            f"Within all limits: risk ${intent.max_loss:,.2f}, "
            f"{state.open_positions + 1}/{cfg.max_positions} positions",
            approved_contracts=approved,
        )
