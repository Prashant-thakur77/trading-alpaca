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
from pathlib import Path


class PromptCache:
    """Filesystem-backed cache: prompt_hash -> call record."""

    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, prompt_hash: str) -> Path:
        return self.cache_dir / f"{prompt_hash}.json"

    def get(self, prompt_hash: str) -> dict | None:
        """Return the cached record for this hash, or None on a miss."""
        path = self._path(prompt_hash)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def put(self, prompt_hash: str, record: dict) -> Path:
        """Persist a call record (prompt, model, raw response, parsed
        result, timestamp, error) under this hash. Returns the file path."""
        path = self._path(prompt_hash)
        path.write_text(json.dumps(record, indent=2, sort_keys=True, default=str))
        return path
