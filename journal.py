"""
Hash-chained, append-only decision journal.

Every decision the desk makes — proposal, veto, verdict, fill, exit — appends
exactly one JSONL entry whose `prev_hash` is the SHA-256 of the previous entry.
Editing or deleting any past entry breaks the chain and `verify_chain` says so.
Past entries are never rewritten (CLAUDE.md hard rule 5).

Entry shape:
    {"seq", "timestamp", "type", "payload", "prev_hash", "entry_hash"}

`entry_hash` is SHA-256 over the canonical JSON of every other field, so it
commits to the payload *and* to the chain position.
"""
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# prev_hash of the very first entry — 64 zeros, no predecessor exists.
GENESIS_HASH = "0" * 64


def _canonical(obj: dict) -> bytes:
    """Deterministic JSON encoding — key order and separators fixed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def compute_entry_hash(entry: dict) -> str:
    """SHA-256 over every field except entry_hash itself."""
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


class Journal:
    """Append-only hash-chained JSONL journal."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create on init so an initialized-but-empty journal is distinguishable
        # from a missing one — verify_chain treats a missing file as tampering.
        self.path.touch(exist_ok=True)

    @staticmethod
    def _tail_of(text: str) -> dict | None:
        """Parse the final non-blank line of a journal body, or None if empty."""
        last = None
        for line in text.splitlines():
            if line.strip():
                last = line
        return json.loads(last) if last else None

    def _last_entry(self) -> dict | None:
        """Read the final entry, or None for an empty/absent journal.

        Unlocked — for read-only callers. `append` must NOT use this: it reads
        the tail *inside* the lock, see below.
        """
        if not self.path.exists():
            return None
        return self._tail_of(self.path.read_text())

    def append(self, entry_type: str, payload: dict) -> dict:
        """Append one entry chained to the current tail. Returns the entry.

        Read-then-write is one critical section. The scan process and the exit
        monitor journal concurrently; if the tail were read before the lock was
        taken, both would chain to the same predecessor and emit two entries
        with the same seq and prev_hash. That forks the chain, and because
        entries are never rewritten (hard rule 5) `verify_chain` — a judged
        artifact — would fail permanently. So: open, lock, THEN read.

        Mode "a+" both creates the file if it is missing and forces every
        write to the end, so a writer can never overwrite another's line.
        """
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                previous = self._tail_of(f.read())
                if previous is None:
                    seq, prev_hash = 0, GENESIS_HASH
                else:
                    seq, prev_hash = previous["seq"] + 1, previous["entry_hash"]

                entry = {
                    "seq": seq,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": entry_type,
                    "payload": payload,
                    "prev_hash": prev_hash,
                }
                entry["entry_hash"] = compute_entry_hash(entry)

                f.write(json.dumps(entry, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return entry

    def entries(self) -> list[dict]:
        """All entries in order."""
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]


def verify_chain(path: Path | str) -> tuple[bool, str]:
    """Verify every entry's own hash and its link to its predecessor.

    Returns (ok, error_message). error_message is "" when ok.
    """
    path = Path(path)
    if not path.exists():
        return False, f"Journal not found: {path}"

    expected_prev = GENESIS_HASH
    for i, line in enumerate(ln for ln in path.read_text().splitlines() if ln.strip()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            return False, f"entry {i}: malformed JSON ({e})"

        if entry.get("prev_hash") != expected_prev:
            return False, (
                f"entry {i}: prev_hash mismatch — expected {expected_prev[:16]}…, "
                f"got {str(entry.get('prev_hash'))[:16]}…"
            )

        recomputed = compute_entry_hash(entry)
        if recomputed != entry.get("entry_hash"):
            return False, (
                f"entry {i}: content was modified — entry_hash {str(entry.get('entry_hash'))[:16]}… "
                f"does not match recomputed {recomputed[:16]}…"
            )

        expected_prev = entry["entry_hash"]

    return True, ""
