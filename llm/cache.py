"""Prompt cache — one JSON file per LLM call, keyed by prompt_hash.

This directory is three things at once, deliberately:
  1. A cost saver — a repeated (model, prompt) pair skips a paid `claude -p`
     call entirely.
  2. The audit record — every call the desk ever made is on disk, with the
     exact prompt sent and the exact raw text that came back.
  3. The deterministic-replay corpus — golden-file tests and the /judge page
     replay a past cycle by reading these records back, never by calling an
     LLM again. A cache hit MUST reproduce the record byte-for-byte, because
     replay correctness depends on it.

Failed parses are persisted too (with their error), not silently dropped —
the raw response has to survive so a bad prompt or a bad model day can be
debugged after the fact.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptCache:
    """Filesystem-backed cache: prompt_hash -> call record."""

    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, prompt_hash: str) -> Path:
        return self.cache_dir / f"{prompt_hash}.json"

    def get(self, prompt_hash: str) -> dict | None:
        """Return the cached record for this hash, or None on a miss.

        An unreadable or corrupt record is a MISS, not an exception: the
        /judge replay path and every live cycle read this cache, and a single
        truncated file must not break them permanently. The caller simply
        re-runs the call and overwrites the bad record.
        """
        path = self._path(prompt_hash)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Unreadable cache record %s (%s) — treating as a miss",
                           path.name, e)
            return None
        if not isinstance(record, dict):
            logger.warning("Cache record %s is %s, not an object — treating as a miss",
                           path.name, type(record).__name__)
            return None
        return record

    def put(self, prompt_hash: str, record: dict) -> Path:
        """Persist a call record (prompt, model, raw response, parsed
        result, timestamp, error) under this hash. Returns the file path.

        Written temp-then-rename: `os.replace` is atomic within a directory,
        so a reader never observes a half-written record and an interrupted
        write leaves the previous record intact rather than a truncated file
        that would make every later `get` fail.
        """
        path = self._path(prompt_hash)
        payload = json.dumps(record, indent=2, sort_keys=True, default=str)

        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=f".{prompt_hash}.",
                                        suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return path
