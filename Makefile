.PHONY: site help install test status validate schedule schedule-live validation-artifacts validate-json verify verify-journal walkforward check-account session-dry session session-live calibration seed-calibration judge judge-page clean

help: ## Show available commands
	@echo "Trading Alpaca — AI Trading Agent"
	@echo ""
	@echo "Quick Start:"
	@echo "  make install      Install dependencies"
	@echo "  make test         Run test suite"
	@echo "  make status       Show paper account, options level, positions"
	@echo "  make session-dry  Run one full cycle, no order sent"
	@echo "  make validate     Validation audit report (for judges)"
	@echo "  make validate-json  Audit report as JSON"
	@echo "  make verify       Merkle root over artifacts derived from the journal"
	@echo "  make verify-journal  Verify hash chain of the decision journal"
	@echo "  make calibration  Per-analyst Brier calibration report"
	@echo "  make seed-calibration  Replay the committee over post-cutoff history"
	@echo "  make check-account   Verify Alpaca paper account is set up correctly"
	@echo "  make walkforward  Run walk-forward OOS validation (needs Alpaca keys)"
	@echo "  make session      Run one session cycle (safe default: no order sent)"
	@echo "  make session-live Run one session cycle, guarded execution LIVE (submits real orders)"
	@echo "  make schedule     Loop one cycle per 30 min in market hours (dry)"
	@echo "  make schedule-live Same, armed: submits real paper orders"
	@echo "  make judge        Replay the 4 credential-free judge scenarios"
	@echo "  make clean        Remove caches and logs"

install:
	pip install -r requirements.txt

test:
	python3 -m pytest tests/ -v --tb=short

# The three files under validation/ are a projection of the hash-chained
# journal, derived offline — not a second write path in the live session.
# Rebuilding before reporting is what keeps the Merkle root in `make verify`
# attesting to the current journal rather than to whatever was committed last.
validation-artifacts:
	python3 scripts/build_validation_artifacts.py

validate: validation-artifacts
	python3 validate.py

validate-json: validation-artifacts
	python3 validate.py --json

verify: validation-artifacts
	python3 merkle.py

# `status` and `check-account` are the same preflight: equity, options
# level, open positions. Kept as two names because README and the
# runbook each reference one of them.
status check-account:
	python3 scripts/check_account.py

walkforward:
	python3 scripts/run_walkforward.py

judge:
	python3 scripts/replay.py --all

judge-page:
	python3 scripts/build_judge_page.py

# build_site rebuilds site/ from scratch, so the live-data smile page is
# regenerated after it rather than being wiped by it.
site:
	python3 scripts/build_site.py
	python3 scripts/build_smile_page.py

verify-journal:
	python3 scripts/verify_journal.py

calibration:
	python3 scripts/calibration_report.py

# Replay the REAL committee over REAL post-knowledge-cutoff market data so the
# calibration loop has resolved outcomes to score. Writes a SEPARATE journal
# (logs/seed_journal.jsonl): the live journal records real broker interaction
# and is a judged artifact, so replayed history never touches it. The window
# starts on 2026-06-01 because the model's knowledge cutoff is May 2026 — see
# seed_replay.KNOWLEDGE_CUTOFF and docs/calibration_seeding.md.
seed-calibration:
	python3 scripts/seed_calibration.py --symbol SPY --start 2026-06-01 \
		--end 2026-08-07 --spacing 1 --max-windows 50
	python3 scripts/calibration_report.py --journal logs/seed_journal.jsonl

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

# One cycle per aligned 30-minute slot, regular trading hours only. A single
# cycle reaches an executable trade ~28% of the time; across a session of
# slots that compounds to ~99%, which is the whole point of running it.
# Safe by default, exactly like `session`: this target submits nothing.
schedule:
	python3 scripts/scheduler.py --wait-for-open

# Armed. Deliberately requires the long second flag: one flag is a typo,
# two is a decision. See the 2026-08-29 incident note above `session`.
schedule-live:
	python3 scripts/scheduler.py --wait-for-open --live \
		--i-understand-this-submits-real-orders

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache logs/*.log
