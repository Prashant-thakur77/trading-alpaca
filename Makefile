.PHONY: help install test status scan dry-run run reset validate validate-json verify verify-journal walkforward check-account session-dry session session-live calibration judge clean

help: ## Show available commands
	@echo "Trading Alpaca — AI Trading Agent"
	@echo ""
	@echo "Quick Start:"
	@echo "  make install      Install dependencies"
	@echo "  make test         Run test suite"
	@echo "  make status       Show agent status and paper balance"
	@echo "  make scan         Run single scan (test signals)"
	@echo "  make dry-run      See signals without executing"
	@echo "  make run          Start main loop (4H scan + 5min monitor)"
	@echo "  make validate     Validation audit report (for judges)"
	@echo "  make validate-json  Audit report as JSON"
	@echo "  make verify       Verify Merkle integrity of validation artifacts"
	@echo "  make verify-journal  Verify hash chain of the decision journal"
	@echo "  make calibration  Per-analyst Brier calibration report"
	@echo "  make check-account   Verify Alpaca paper account is set up correctly"
	@echo "  make walkforward  Run walk-forward OOS validation (needs Alpaca keys)"
	@echo "  make session-dry  Run one session cycle, no order sent"
	@echo "  make session      Run one session cycle (safe default: no order sent)"
	@echo "  make session-live Run one session cycle, guarded execution LIVE (submits real orders)"
	@echo "  make judge        Replay the 4 credential-free judge scenarios"
	@echo "  make reset        Reset paper balance to \$$100,000"
	@echo "  make clean        Remove caches and logs"

install:
	pip install -r requirements.txt

test:
	python3 -m pytest tests/ -v --tb=short

status:
	python3 agent.py --status

scan:
	python3 agent.py --single-scan

dry-run:
	python3 agent.py --dry-run

run:
	python3 agent.py

reset:
	python3 agent.py --reset

validate:
	python3 validate.py

validate-json:
	python3 validate.py --json

verify:
	python3 merkle.py

check-account:
	python3 scripts/check_account.py

walkforward:
	python3 scripts/run_walkforward.py

verify-journal:
	python3 scripts/verify_journal.py

calibration:
	python3 scripts/calibration_report.py

session-dry:
	python3 scripts/run_session.py --dry-run

# Safe by design: this target must NEVER submit an order, because it is the
# shortest, most likely to be typed — or accidentally triggered — command
# (2026-08-29: a shell-expanded backtick in an unrelated `git commit -m`
# message ran exactly this target and submitted a real paper order nobody
# intended to send). It runs the complete pipeline and stops before
# submission, identically to `make session-dry`. Use `make session-live` to
# actually send an order.
session:
	python3 scripts/run_session.py

session-live:
	python3 scripts/run_session.py --live

judge:
	python3 scripts/replay.py --all

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache logs/*.log
