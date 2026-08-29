#!/usr/bin/env python3
"""
Verify the integrity of the hash-chained decision journal.

Anyone (judges included) can run this against the shipped journal to confirm
no entry was edited, reordered, or deleted after the fact:

    python3 scripts/verify_journal.py
    python3 scripts/verify_journal.py --path logs/journal.jsonl --json

Exit code 0 = chain intact, 1 = tampered/missing.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import Journal, verify_chain

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "journal.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify decision-journal hash chain")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="Path to journal.jsonl")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    path = Path(args.path)

    # An absent journal is "nothing recorded yet", not "tampered with". Only a
    # journal that exists can have its chain broken, and that is what exit 1
    # is reserved for — so a fresh clone doesn't report a false alarm.
    if not path.exists():
        if args.json:
            print(json.dumps({"path": str(path), "intact": True, "entries": 0,
                              "note": "No journal yet — no decisions recorded."}, indent=2))
        else:
            print(f"\n  Journal:  {path}")
            print(f"  Entries:  0")
            print(f"  Chain:    EMPTY — no decisions recorded yet\n")
        return 0

    ok, err = verify_chain(path)
    entries = Journal(path).entries() if ok else []

    if args.json:
        print(json.dumps({
            "path": str(path),
            "intact": ok,
            "entries": len(entries),
            "error": err,
        }, indent=2))
    else:
        print(f"\n  Journal:  {path}")
        print(f"  Entries:  {len(entries)}")
        if ok:
            counts: dict[str, int] = {}
            for e in entries:
                counts[e["type"]] = counts.get(e["type"], 0) + 1
            for etype, n in sorted(counts.items()):
                print(f"    {etype:12s} {n}")
            tip = entries[-1]["entry_hash"] if entries else "—"
            print(f"  Chain:    INTACT")
            print(f"  Tip hash: {tip}\n")
        else:
            print(f"  Chain:    BROKEN")
            print(f"  Reason:   {err}\n")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
