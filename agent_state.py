"""
Agent state management — save/load for crash recovery.

Extracted from agent.py to keep main agent under 800 lines.
All functions are standalone helpers that operate on the agent instance (passed as `agent`).
"""
import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Re-use paths from agent.py (will be set by caller)
LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_PATH = LOG_DIR / "trade_log.jsonl"
STATE_PATH = LOG_DIR / "agent_state.json"


def save_state(agent) -> None:
    """Save agent state to disk for crash recovery.

    Uses file-level locking (fcntl) to prevent race conditions
    between scan and monitor cron processes.
    """
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scan_count": agent.scan_count,
        "last_full_scan": agent.last_full_scan,
        "open_positions": len(agent.executor.positions),
        "positions": {
            pair: {
                "cli_pair": pos.cli_pair,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "volume": pos.volume,
                "sl_price": pos.sl_price,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "tp3_price": pos.tp3_price,
                "tp1_hit": pos.tp1_hit,
                "tp2_hit": pos.tp2_hit,
                "remaining_pct": pos.remaining_pct,
                "source": pos.source,
                "opened_at": pos.opened_at.isoformat(),
            }
            for pair, pos in agent.executor.positions.items()
        },
        "risk": {
            "total_realized_pnl": agent.risk.total_realized_pnl,
            "peak_balance": agent.risk.peak_balance,
            "consecutive_losses": agent.risk.consecutive_losses,
            "position_scale": agent.risk.position_scale,
            "daily_date": agent.risk.daily_stats.date,
            "daily_trades": agent.risk.daily_stats.trades_count,
            "daily_pnl": agent.risk.daily_stats.realized_pnl,
            "daily_stopped": agent.risk.daily_stats.is_stopped,
            "daily_stop_reason": agent.risk.daily_stats.stop_reason,
            "pair_consecutive_losses": agent.risk.pair_consecutive_losses,
            "pair_cooldown": agent.risk.pair_cooldown,
        },
    }
    # Atomic write with file lock to prevent race conditions
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=2, default=str)
        f.flush()
    tmp_path.rename(STATE_PATH)  # Atomic rename


def load_state(agent) -> None:
    """Restore agent state from disk after restart."""
    if not STATE_PATH.exists():
        return

    try:
        with open(STATE_PATH) as f:
            fcntl.flock(f, fcntl.LOCK_SH)  # Shared lock for reads
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load state: {e}")
        return

    # Restore scan tracking
    agent.scan_count = state.get("scan_count", 0)
    agent.last_full_scan = state.get("last_full_scan", 0)

    # Restore positions
    from executor import Position
    positions_data = state.get("positions", {})
    for pair, pdata in positions_data.items():
        opened_at = datetime.fromisoformat(pdata["opened_at"]) if pdata.get("opened_at") else datetime.now(timezone.utc)
        pos = Position(
            pair=pair,
            cli_pair=pdata["cli_pair"],
            direction=pdata["direction"],
            entry_price=pdata["entry_price"],
            volume=pdata["volume"],
            sl_price=pdata["sl_price"],
            tp1_price=pdata["tp1_price"],
            tp2_price=pdata.get("tp2_price", 0),
            tp3_price=pdata.get("tp3_price", 0),
            opened_at=opened_at,
            source=pdata.get("source", ""),
            tp1_hit=pdata.get("tp1_hit", False),
            tp2_hit=pdata.get("tp2_hit", False),
            remaining_pct=pdata.get("remaining_pct", 1.0),
        )
        agent.executor.positions[pair] = pos

    # Restore risk state
    risk_data = state.get("risk", {})
    agent.risk.total_realized_pnl = risk_data.get("total_realized_pnl", 0)
    agent.risk.peak_balance = risk_data.get("peak_balance", agent.risk.initial_capital)
    agent.risk.consecutive_losses = risk_data.get("consecutive_losses", 0)
    agent.risk.position_scale = risk_data.get("position_scale", 1.0)
    agent.risk.open_position_count = len(positions_data)

    # Restore per-pair risk state
    agent.risk.pair_consecutive_losses = risk_data.get("pair_consecutive_losses", {})
    agent.risk.pair_cooldown = risk_data.get("pair_cooldown", {})

    # Restore daily stats if same day
    if risk_data.get("daily_date") == agent.risk._today():
        agent.risk.daily_stats.trades_count = risk_data.get("daily_trades", 0)
        agent.risk.daily_stats.realized_pnl = risk_data.get("daily_pnl", 0)
        agent.risk.daily_stats.is_stopped = risk_data.get("daily_stopped", False)
        agent.risk.daily_stats.stop_reason = risk_data.get("daily_stop_reason", "")

    restored = len(positions_data)
    if restored > 0:
        logger.info(f"State restored: {restored} positions, total PnL ${agent.risk.total_realized_pnl:+.2f}")
    else:
        logger.info("State loaded (no open positions)")
