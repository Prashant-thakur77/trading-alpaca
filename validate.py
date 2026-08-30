#!/usr/bin/env python3
"""
Validation Report Generator — Summarize all validation artifacts for judges.

Reads trade_intents.json, risk_checks.json, strategy_checkpoints.json
and generates a human-readable audit report.

Usage:
  python3 validate.py              # Full report
  python3 validate.py --json       # Machine-readable JSON summary
"""
import argparse
import json
import sys
from pathlib import Path

from merkle import compute_artifact_merkle

VALIDATION_DIR = Path(__file__).parent / "validation"
WALKFORWARD_PATH = VALIDATION_DIR / "walkforward.json"


def load_walkforward_results() -> dict:
    """Load computed walk-forward results, if a run has produced any.

    Returns {"available": False, ...} when none exist. This report never
    prints a performance figure that was not computed from real bars — an
    absent number is reported as absent.
    """
    if not WALKFORWARD_PATH.exists():
        return {
            "available": False,
            "note": "No walk-forward run on record. Run: make walkforward",
        }
    try:
        data = json.loads(WALKFORWARD_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"available": False, "note": f"Walk-forward results unreadable: {e}"}

    data["available"] = bool(data.get("symbols"))
    if not data["available"]:
        data["note"] = "Walk-forward file present but contains no symbol results."
    return data

ARTIFACT_FILES = {
    "trade_intents": VALIDATION_DIR / "trade_intents.json",
    "risk_checks": VALIDATION_DIR / "risk_checks.json",
    "strategy_checkpoints": VALIDATION_DIR / "strategy_checkpoints.json",
}


def load_artifact(path: Path) -> dict:
    """Load a validation artifact JSON file."""
    if not path.exists():
        return {"records": [], "error": f"File not found: {path}"}
    with open(path) as f:
        return json.load(f)


def analyze_trade_intents(data: dict) -> dict:
    """Analyze committee decisions projected from the journal.

    The crypto-era version of this counted pairs, regimes and long/short
    directions. None of those exist for a defined-risk options desk, and the
    file it read was never written. It now summarises what the committee
    actually produced: a structure, a probability, and an outcome.
    """
    records = data.get("records", [])
    if not records:
        return {"count": 0}

    underlyings, structures = set(), set()
    outcomes: dict[str, int] = {}
    probs, pnls = [], []

    for r in records:
        if r.get("underlying"):
            underlyings.add(r["underlying"])
        if r.get("structure"):
            structures.add(r["structure"])
        outcomes[r.get("outcome", "UNKNOWN")] = outcomes.get(r.get("outcome", "UNKNOWN"), 0) + 1
        if isinstance(r.get("aggregateProbability"), (int, float)):
            probs.append(r["aggregateProbability"])
        if isinstance(r.get("realizedPnl"), (int, float)):
            pnls.append(r["realizedPnl"])

    abstained = outcomes.get("ABSTAINED", 0) + outcomes.get("VETOED", 0)
    return {
        "count": len(records),
        "underlyings": sorted(underlyings),
        "structures": sorted(structures),
        "outcomes": outcomes,
        "abstentionRate": round(abstained / len(records) * 100, 1),
        "meanProbability": round(sum(probs) / len(probs), 3) if probs else None,
        "resolved": len(pnls),
        "realizedPnl": round(sum(pnls), 2) if pnls else 0.0,
        "winners": sum(1 for p in pnls if p > 0),
    }


def analyze_risk_checks(data: dict) -> dict:
    """Analyze the two refusal gates: RiskGuard and the dual-model veto.

    RiskGuard is three-valued (ALLOW / ALLOW_WITH_DOWNSIZE / DENY), not the
    crypto agent's five boolean layers, so this reports verdict counts per
    gate instead of a per-layer pass/fail matrix.
    """
    records = data.get("records", [])
    if not records:
        return {"count": 0}

    by_gate: dict[str, dict[str, int]] = {}
    refused = 0
    for r in records:
        gate = r.get("gate", "unknown")
        decision = r.get("decision", "UNKNOWN")
        by_gate.setdefault(gate, {})
        by_gate[gate][decision] = by_gate[gate].get(decision, 0) + 1
        if decision in ("DENY", "VETO"):
            refused += 1

    return {
        "count": len(records),
        "gates": data.get("gates", sorted(by_gate)),
        "byGate": by_gate,
        "refused": refused,
        "allowed": len(records) - refused,
        "refusalRate": round(refused / len(records) * 100, 1),
    }


def analyze_strategy_checkpoints(data: dict) -> dict:
    """Analyze what the desk saw before each decision.

    Replaces the crypto agent's regime-routing counters, which described a
    trend/range classifier this project does not have. The funnel below is
    the decision-relevant fact: how much of the chain survived to be shown.
    """
    records = data.get("records", [])
    if not records:
        return {"count": 0}

    underlyings = set()
    total_built = shown = 0
    vols = []
    for r in records:
        if r.get("underlying"):
            underlyings.add(r["underlying"])
        if isinstance(r.get("totalCandidates"), int):
            total_built += r["totalCandidates"]
        if isinstance(r.get("shownToCommittee"), int):
            shown += r["shownToCommittee"]
        if isinstance(r.get("realizedVol"), (int, float)):
            vols.append(r["realizedVol"])

    return {
        "count": len(records),
        "underlyings": sorted(underlyings),
        "candidatesBuilt": total_built,
        "shownToCommittee": shown,
        "shownPct": round(shown / total_built * 100, 2) if total_built else None,
        "meanRealizedVol": round(sum(vols) / len(vols), 4) if vols else None,
    }


def generate_report(as_json: bool = False) -> dict:
    """Generate comprehensive validation report."""
    artifacts = {}
    for name, path in ARTIFACT_FILES.items():
        artifacts[name] = load_artifact(path)

    ti_analysis = analyze_trade_intents(artifacts["trade_intents"])
    rc_analysis = analyze_risk_checks(artifacts["risk_checks"])
    sc_analysis = analyze_strategy_checkpoints(artifacts["strategy_checkpoints"])

    total_records = ti_analysis["count"] + rc_analysis["count"] + sc_analysis["count"]

    # Compute Merkle integrity hash
    merkle = compute_artifact_merkle()

    report = {
        "title": "Trading Alpaca — Validation Audit Report",
        "totalRecords": total_records,
        "integrity": {
            "merkleRoot": merkle["merkle_root"],
            "algorithm": merkle["algorithm"],
            "treeDepth": merkle["tree_depth"],
            "perFileRoots": {
                name: info.get("merkle_root", "N/A")
                for name, info in merkle["files"].items()
            },
            "verificationCommand": "python3 merkle.py  # Recompute and compare root hash",
        },
        "tradeIntents": ti_analysis,
        "riskChecks": rc_analysis,
        "strategyCheckpoints": sc_analysis,
        "walkForward": load_walkforward_results(),
        "refusalControl": {
            "gates": len(rc_analysis.get("byGate", {})),
            "refusalRate": f"{rc_analysis.get('refusalRate', 0)}%",
            "abstentionRate": f"{ti_analysis.get('abstentionRate', 0)}%",
        },
        "validationQuality": {
            "totalArtifacts": total_records,
            "auditTrail": "Every decision logged with full context",
        },
    }

    if as_json:
        return report

    # Pretty-print text report
    print(f"\n{'='*70}")
    print(f"  {report['title']}")
    print(f"{'='*70}")
    print(f"\n  Total Validation Records: {total_records}")

    print(f"\n{'─'*70}")
    print(f"  INTEGRITY VERIFICATION (Merkle Tree)")
    print(f"{'─'*70}")
    print(f"  Merkle Root:  {merkle['merkle_root']}")
    print(f"  Algorithm:    {merkle['algorithm']}")
    print(f"  Tree Depth:   {merkle['tree_depth']}")
    for fname, info in merkle["files"].items():
        root = info.get("merkle_root", "N/A")
        count = info.get("records", 0)
        print(f"  {fname:35s} {count:3d} records  root: {root[:16]}...")
    print(f"\n  To verify: python3 merkle.py")

    print(f"\n{'─'*70}")
    print(f"  TRADE INTENTS ({ti_analysis['count']} records)")
    print(f"{'─'*70}")
    print(f"  Underlyings:     {', '.join(ti_analysis.get('underlyings', [])) or 'none'}")
    print(f"  Structures:      {', '.join(ti_analysis.get('structures', [])) or 'none'}")
    print(f"  Outcomes:        {ti_analysis.get('outcomes', {})}")
    print(f"  Abstention rate: {ti_analysis.get('abstentionRate', 0)}%")
    prob = ti_analysis.get("meanProbability")
    print(f"  Mean probability: {prob if prob is not None else 'n/a'}")

    resolved = ti_analysis.get("resolved", 0)
    if resolved:
        print(f"\n  Resolved outcomes (replayed history, not live fills):")
        print(f"    Resolved:      {resolved}")
        print(f"    Winners:       {ti_analysis.get('winners', 0)}")
        print(f"    Realized PnL:  ${ti_analysis.get('realizedPnl', 0.0):+.2f}")

    print(f"\n{'─'*70}")
    print(f"  RISK CHECKS ({rc_analysis['count']} records)")
    print(f"{'─'*70}")
    for gate, decisions in rc_analysis.get("byGate", {}).items():
        print(f"  {gate}: {decisions}")
    print(f"\n  Allowed:      {rc_analysis.get('allowed', 0)}")
    print(f"  Refused:      {rc_analysis.get('refused', 0)}")
    print(f"  Refusal rate: {rc_analysis.get('refusalRate', 0)}%")

    print(f"\n{'─'*70}")
    print(f"  STRATEGY CHECKPOINTS ({sc_analysis['count']} records)")
    print(f"{'─'*70}")
    print(f"  Underlyings:        {', '.join(sc_analysis.get('underlyings', [])) or 'none'}")
    print(f"  Candidates built:   {sc_analysis.get('candidatesBuilt', 0)}")
    print(f"  Shown to committee: {sc_analysis.get('shownToCommittee', 0)}"
          f"  ({sc_analysis.get('shownPct')}% of built)")
    mrv = sc_analysis.get("meanRealizedVol")
    print(f"  Mean realized vol:  {mrv if mrv is not None else 'n/a'}")

    print(f"\n{'─'*70}")
    print(f"  WALK-FORWARD OUT-OF-SAMPLE RESULTS")
    print(f"{'─'*70}")
    wf = report["walkForward"]
    if not wf.get("available"):
        # Never print a performance number we have not computed.
        print(f"  {wf.get('note', 'No walk-forward results available.')}")
    else:
        print(f"  Generated: {wf.get('generated_at', '')}")
        print(f"  Windows:   IS={wf.get('is_bars')} bars / OOS={wf.get('oos_bars')} bars\n")
        print(f"  {'symbol':8s} {'windows':>8s} {'trades':>7s} {'win%':>7s} "
              f"{'expR':>7s} {'PF':>7s} {'maxDD_R':>8s}")
        for sym, r in wf.get("symbols", {}).items():
            o = r.get("oos", {})
            pf = o.get("profit_factor", 0.0)
            pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
            print(f"  {sym:8s} {r.get('windows', 0):8d} {o.get('trades', 0):7d} "
                  f"{o.get('win_rate', 0):7.1f} {o.get('expectancy_r', 0):7.2f} "
                  f"{pf_s:>7s} {o.get('max_drawdown_r', 0):8.2f}")

    print(f"\n{'─'*70}")
    print(f"  REFUSAL CONTROL")
    print(f"{'─'*70}")
    for k, v in report["refusalControl"].items():
        print(f"    {k:20s} {v}")
    print(f"\n  Validation Quality:")
    for k, v in report["validationQuality"].items():
        print(f"    {k:20s} {v}")
    print()

    return report


def main():
    parser = argparse.ArgumentParser(description="Validation Report Generator")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    report = generate_report(as_json=args.json)
    if args.json:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
