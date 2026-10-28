"""Local performance history for the Perf HUD plugin.

One JSON file per profile, owned by the plugin, capped at 50 sessions by
default. Every record goes through :data:`RECORD_FIELDS` -- an allow-list --
so a future hook cannot widen what is persisted by accident. Prompts,
responses, tool arguments and tool results have no field to land in.

The store never raises into the host: a corrupt file is quarantined and
replaced on the next write, and a failed write is reported through ``last_error``
so the UI can show it instead of the plugin taking the session down.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MAX_SESSIONS = 50

#: The only keys that may reach disk.
RECORD_FIELDS = (
    "session_id",
    "model",
    "provider",
    "started_at",
    "updated_at",
    "duration_ms",
    "ttft_ms",
    "tps_core",
    "input_tokens",
    "output_tokens",
    "cache_hit_pct",
    "tool_time_ms",
    "llm_calls",
)


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the allow-listed, JSON-safe fields of *record*."""
    out: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        value = record.get(key)
        if isinstance(value, (int, float, str)) or value is None:
            out[key] = value
    if not out.get("session_id"):
        return {}
    out.setdefault("updated_at", time.time())
    return out


class HistoryStore:
    """A capped, newest-first list of session performance records."""

    def __init__(self, path: Path, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self.path = Path(path)
        self.max_sessions = max(1, int(max_sessions))
        self.last_error: str = ""

    # ------------------------------------------------------------------ read

    def _read_all(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self._quarantine(exc)
            return []
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            self._quarantine(ValueError("sessions is not a list"))
            return []
        return [rec for rec in (_clean(item) for item in sessions if isinstance(item, dict)) if rec]

    def _quarantine(self, exc: Exception) -> None:
        """Move an unreadable file aside instead of losing the session over it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
        except OSError:
            pass
        self.last_error = f"history unreadable ({exc}); started a new file"

    def load(self) -> list[dict[str, Any]]:
        return sorted(self._read_all(), key=lambda rec: rec.get("updated_at") or 0, reverse=True)

    def get(self, session_id: str) -> dict[str, Any] | None:
        for record in self._read_all():
            if record.get("session_id") == session_id:
                return record
        return None

    # ----------------------------------------------------------------- write

    def _write(self, sessions: Iterable[dict[str, Any]]) -> bool:
        payload = {"version": 1, "max_sessions": self.max_sessions, "sessions": list(sessions)}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            self.last_error = f"history write failed: {exc}"
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        self.last_error = ""
        return True

    def upsert(self, record: dict[str, Any]) -> bool:
        """Insert or refresh one session record, keeping the newest *max* ones."""
        clean = _clean(record)
        if not clean:
            return False
        sessions = [rec for rec in self._read_all() if rec.get("session_id") != clean["session_id"]]
        sessions.insert(0, clean)
        sessions.sort(key=lambda rec: rec.get("updated_at") or 0, reverse=True)
        return self._write(sessions[: self.max_sessions])

    def delete(self, session_id: str) -> bool:
        sessions = self._read_all()
        kept = [rec for rec in sessions if rec.get("session_id") != session_id]
        if len(kept) == len(sessions):
            return True
        return self._write(kept)

    def clear(self) -> bool:
        return self._write([])

    # ------------------------------------------------------------------ view

    def stats(self) -> dict[str, Any]:
        sessions = self._read_all()
        return {
            "sessions": len(sessions),
            "max_sessions": self.max_sessions,
            "path": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "last_error": self.last_error,
        }
