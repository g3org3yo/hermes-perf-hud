"""Pure aggregation layer for the Perf HUD plugin.

Events in, snapshot dict out. Nothing here touches the filesystem, the network,
the clock, or Hermes internals, which is what makes the numbers unit-testable
without an engine running.

Metric contract
---------------
``tps_core``
    Hermes' own live throughput gauge: ``sum(output_tokens) /
    sum(api_duration)`` over the last 10 completed API calls. The core keeps
    ``_api_latency_history`` / ``_api_output_history`` as ``deque(maxlen=10)``
    and the CLI status bar divides their sums (``agent/turn_usage.py``,
    ``hermes_cli/cli_status_bar_mixin.py``). Same window, same arithmetic, so
    this value and the core meter agree.

``session_avg_tps``
    The same ratio over *every* call in the session. A different metric. The
    UI labels it "Session Avg TPS" and never shows it as "TPS".

``llm_calls``
    One per completed API request (success or error). Hermes has no independent
    step counter, so the UI shows this as "LLM calls" and, for layouts that
    want a step field, "Agent steps*" with the footnote in
    :data:`AGENT_STEPS_NOTE`.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Optional

CORE_WINDOW = 10
AGENT_STEPS_NOTE = "Equivalent to LLM calls in the current Hermes telemetry API."


def _num(value: Any) -> float:
    """Best-effort float; observers pass ints, floats, Decimal or None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(_num(value))


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator > 0 else None


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


class SessionAggregate:
    """Performance counters for one session.

    Not thread-safe on its own: the hook layer owns the lock, so this class
    stays free of locking policy and easy to test.
    """

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        # (output_tokens, api_duration) for the last CORE_WINDOW calls, in the
        # order the core appends them: latency first, then output tokens.
        self._core_window: Deque[tuple[float, float]] = deque(maxlen=CORE_WINDOW)
        self.llm_calls = 0
        self.api_errors = 0
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.reasoning_tokens = 0
        self.total_api_ms = 0.0
        self.total_output_tokens = 0
        self.ttft_ms: list[float] = []
        self.tool_calls = 0
        self.tool_errors = 0
        self.tool_time_ms = 0.0
        self.tools: dict[str, dict[str, float]] = {}
        self.model = ""
        self.provider = ""
        self.first_at: Optional[float] = None
        self.last_at: Optional[float] = None
        self.last_error: Optional[dict[str, Any]] = None
        self.started_monotonic = False

    # ---------------------------------------------------------------- events

    def note_model(self, model: str = "", provider: str = "") -> None:
        if model:
            self.model = model
        if provider:
            self.provider = provider

    def on_api_success(
        self,
        *,
        usage: Optional[dict[str, Any]] = None,
        api_duration: float = 0.0,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        first_chunk_at: Optional[float] = None,
        model: str = "",
        provider: str = "",
        response_model: str = "",
    ) -> None:
        """Record one completed provider call (the post_api_request payload)."""
        usage = usage if isinstance(usage, dict) else {}
        duration = max(0.0, _num(api_duration))
        out_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))

        self.llm_calls += 1
        self.output_tokens += out_tokens
        self.input_tokens += _int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        self.cache_read_tokens += _int(usage.get("cache_read_tokens"))
        self.cache_write_tokens += _int(usage.get("cache_write_tokens"))
        self.reasoning_tokens += _int(usage.get("reasoning_tokens"))
        self.total_api_ms += duration * 1000.0

        # Core window: the CLI meter counts every completed call, so a
        # tool-call-only turn (0 output tokens, real latency) pulls the average
        # down in both places. Keep the arithmetic identical.
        self._core_window.append((out_tokens, duration))
        self.total_output_tokens += out_tokens

        if first_chunk_at is not None and started_at is not None:
            ttft = (float(first_chunk_at) - float(started_at)) * 1000.0
            if ttft > 0:
                self.ttft_ms.append(ttft)

        self._touch()
        self.note_model(response_model or model, provider)

    def on_api_error(
        self,
        *,
        api_duration: float = 0.0,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        model: str = "",
        provider: str = "",
        error: Optional[dict[str, Any]] = None,
        reason: str = "",
        status_code: Any = None,
    ) -> None:
        """Record a failed attempt. Counts as an LLM call; never as tokens."""
        self.llm_calls += 1
        self.api_errors += 1
        self.total_api_ms += max(0.0, _num(api_duration)) * 1000.0
        self._touch()
        self.note_model(model, provider)
        kind = ""
        message = ""
        if isinstance(error, dict):
            kind = str(error.get("type") or "")
            message = str(error.get("message") or "")
        # Classifier strings only: never the prompt, never the full response.
        self.last_error = {
            "kind": kind or str(reason or "error")[:80],
            "message": message[:160],
            "status": status_code if isinstance(status_code, int) else None,
            "at": self.last_at,
        }

    def on_turn(self) -> None:
        self.turns += 1
        self._touch()

    def on_tool_call(self, *, tool_name: str = "", duration_ms: Any = 0, status: str = "ok") -> None:
        name = (tool_name or "unknown")[:60]
        slot = self.tools.setdefault(name, {"calls": 0.0, "ms": 0.0, "errors": 0.0})
        slot["calls"] += 1
        slot["ms"] += max(0.0, _num(duration_ms))
        slot["errors"] += 0.0 if status == "ok" else 1.0
        self.tool_calls += 1
        self.tool_time_ms += max(0.0, _num(duration_ms))
        self._touch()
        if status != "ok":
            self.tool_errors += 1

    def touch(self) -> None:
        self.started_monotonic = True

    def _touch(self, started_at: Optional[float] = None, ended_at: Optional[float] = None) -> None:
        """Stamp session wall time.

        ``started_at``/``ended_at`` from a hook payload describe one API call,
        not the session; using them here would make the session duration read
        shorter than its own tool time (tools run between calls). Wall clock is
        the only honest source for "total duration", so the API timestamps are
        used solely for TTFT.
        """
        now = time.time()
        self.first_at = now if self.first_at is None else min(self.first_at, now)
        self.last_at = now if self.last_at is None else max(self.last_at, now)

    # -------------------------------------------------------------- snapshot

    def tps_core(self) -> Optional[float]:
        if not self._core_window:
            return None
        tokens = sum(tokens for tokens, _ in self._core_window)
        seconds = sum(duration for _, duration in self._core_window)
        return _ratio(tokens, seconds)

    def session_avg_tps(self) -> Optional[float]:
        return _ratio(self.total_output_tokens, self.total_api_ms / 1000.0)

    def cache_hit_pct(self) -> Optional[float]:
        read = self.cache_read_tokens
        fresh = self.input_tokens
        total = read + fresh
        if total <= 0:
            return None
        return 100.0 * read / total

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe view. Contains no prompts, responses, arguments or results."""
        duration_ms = None
        if self.first_at is not None and self.last_at is not None:
            duration_ms = max(0.0, (self.last_at - self.first_at) * 1000.0)
        return {
            "session_id": self.session_id,
            "model": self.model,
            "provider": self.provider,
            # Which calls are in the core-compatible window, for the tooltip.
            "tps_core": _round(self.tps_core(), 1),
            "tps_core_window": len(self._core_window),
            "tps_core_window_max": CORE_WINDOW,
            "session_avg_tps": _round(self.session_avg_tps(), 1),
            "llm_calls": self.llm_calls,
            "agent_steps": self.llm_calls,
            "agent_steps_note": AGENT_STEPS_NOTE,
            "turns": self.turns,
            "api_errors": self.api_errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_hit_pct": _round(self.cache_hit_pct(), 0),
            "ttft_ms": _round(_percentile(self.ttft_ms, 0.5), 0),
            "ttft_ms_avg": _round(_ratio(sum(self.ttft_ms), len(self.ttft_ms)), 0),
            "ttft_samples": len(self.ttft_ms),
            "api_ms_avg": _round(_ratio(self.total_api_ms, self.llm_calls), 0),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_time_ms": _round(self.tool_time_ms, 0),
            "tool_time_share_pct": _round(
                100.0 * (_ratio(self.tool_time_ms, self.tool_time_ms + self.total_api_ms) or 0.0), 0
            ),
            "tools": sorted(
                (
                    {"name": name, "calls": int(slot["calls"]), "ms": round(slot["ms"]), "errors": int(slot["errors"])}
                    for name, slot in self.tools.items()
                ),
                key=lambda item: (-item["calls"], -item["ms"]),
            ),
            "duration_ms": _round(duration_ms, 0),
            "first_at": self.first_at,
            "last_at": self.last_at,
            "last_error": self.last_error,
        }

    def history_record(self) -> dict[str, Any]:
        """The privacy-bounded slice that may be written to disk.

        Allow-list by construction: only performance metadata leaves the
        process. Prompts, responses, tool arguments and tool results are never
        stored -- they are not even fields of this class.
        """
        snap = self.snapshot()
        return {
            "session_id": snap["session_id"],
            "model": snap["model"],
            "provider": snap["provider"],
            "started_at": snap["first_at"],
            "updated_at": snap["last_at"],
            "duration_ms": snap["duration_ms"],
            "ttft_ms": snap["ttft_ms"],
            "tps_core": snap["tps_core"],
            "input_tokens": snap["input_tokens"],
            "output_tokens": snap["output_tokens"],
            "cache_hit_pct": snap["cache_hit_pct"],
            "tool_time_ms": snap["tool_time_ms"],
            "llm_calls": snap["llm_calls"],
        }

    def reset(self) -> None:
        """Drop this session's counters. Touches nothing outside this object."""
        fresh = SessionAggregate(self.session_id)
        self.__dict__.update(fresh.__dict__)


def merge_snapshots(aggregates: list[SessionAggregate]) -> dict[str, Any]:
    """Fleet view across sessions; TPS fields stay None (no shared window)."""
    totals = {
        "sessions": len(aggregates),
        "llm_calls": sum(a.llm_calls for a in aggregates),
        "turns": sum(a.turns for a in aggregates),
        "output_tokens": sum(a.output_tokens for a in aggregates),
        "input_tokens": sum(a.input_tokens for a in aggregates),
        "tool_calls": sum(a.tool_calls for a in aggregates),
        "tool_time_ms": round(sum(a.tool_time_ms for a in aggregates)),
        "api_errors": sum(a.api_errors for a in aggregates),
    }
    output = sum(a.total_output_tokens for a in aggregates)
    seconds = sum(a.total_api_ms for a in aggregates) / 1000.0
    totals["session_avg_tps"] = _round(_ratio(output, seconds), 1)
    totals["tps_core"] = None
    return totals
