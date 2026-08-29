.PHONY: help install test status scan dry-run run reset validate validate-json verify verify-journal walkforward clean

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
	@echo "  make walkforward  Run walk-forward OOS validation (needs Alpaca keys)"
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

walkforward:
	python3 scripts/run_walkforward.py

verify-journal:
	python3 scripts/verify_journal.py

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache logs/*.log
