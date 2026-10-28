"""Shared runtime: the one live registry both halves of the plugin talk to.

The hook layer (``__init__.py``) feeds events in; the dashboard API
(``dashboard/plugin_api.py``) reads snapshots and applies the reset / clear
controls. Those are two separately imported files, so they must agree on a
single module instance -- hence the fixed-name module loader below instead of
``import runtime`` (which would depend on the plugin directory being on
``sys.path``).

Nothing here imports Hermes core modules except ``plugin_events`` (public, for
the browser bridge), and every call into it is guarded: a host without the
bridge still gets a working plugin.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "perf-hud"
EVENT_STATS = "stats.update"

DEFAULTS: dict[str, Any] = {
    # "tps" | "tps_ttft" | "calls" -- what the status-bar chip shows at rest.
    "chip_metric": "tps",
    "chip_enabled": True,
    "history_enabled": True,
    "history_max_sessions": 50,
    "poll_ms": 5000,
    "broadcast_min_interval_ms": 400,
}

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    """Load ``<plugin_dir>/<name>.py`` once, under a stable module name."""
    module_name = f"hermes_perf_hud_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise ImportError(f"perf-hud: cannot load {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


aggregation = _sibling("aggregation")
history = _sibling("history")

SessionAggregate = aggregation.SessionAggregate
HistoryStore = history.HistoryStore

_lock = threading.RLock()
_aggregates: dict[str, SessionAggregate] = {}
_store: Optional[HistoryStore] = None
_ctx: Any = None
_config_get = lambda key, default=None: DEFAULTS.get(key, default)  # noqa: E731
_last_broadcast = 0.0
_persisted_at: dict[str, float] = {}
_broadcasts = 0
_event_errors = 0


# --------------------------------------------------------------------- setup

def configure(ctx: Any = None) -> None:
    """Bind plugin config (and the history path) to the live runtime."""
    global _store, _config_get, _ctx
    if ctx is not None:
        _ctx = ctx
        _config_get = getattr(ctx, "get_config", _config_get)
        try:
            data_dir = ctx.state.data_dir
        except Exception:  # pragma: no cover - host without plugin state
            data_dir = _HERE / "data"
        _store = HistoryStore(Path(data_dir) / "history.json", _max_sessions())
    elif _store is None:
        _store = HistoryStore(_HERE / "data" / "history.json", _max_sessions())


def set_config_value(key: str, value: Any) -> dict[str, Any]:
    """Write one setting through the plugin's own documented config seam."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    setter = getattr(_ctx, "set_config", None)
    if setter is None:
        raise RuntimeError("plugin config is not writable in this host")
    expected = type(DEFAULTS[key])
    if expected is int and isinstance(value, bool):
        raise ValueError(f"{key} expects an integer")
    if not isinstance(value, expected):
        raise ValueError(f"{key} expects {expected.__name__}")
    setter(key, value)
    global _config_get
    _config_get = getattr(_ctx, "get_config", _config_get)
    return {"key": key, "value": config(key)}



def config(key: str) -> Any:
    value = _config_get(key, DEFAULTS.get(key))
    return DEFAULTS.get(key) if value is None else value


def _max_sessions() -> int:
    try:
        return max(1, int(config("history_max_sessions")))
    except (TypeError, ValueError):
        return int(DEFAULTS["history_max_sessions"])


def store() -> HistoryStore:
    global _store
    with _lock:
        if _store is None:
            _store = HistoryStore(_HERE / "data" / "history.json", _max_sessions())
        _store.max_sessions = _max_sessions()
        return _store


def aggregate(session_id: str) -> SessionAggregate:
    key = session_id or "unknown"
    with _lock:
        session = _aggregates.get(key)
        if session is None:
            session = SessionAggregate(key)
            _aggregates[key] = session
        return session


# -------------------------------------------------------------------- events

def _pick(event: dict[str, Any], *names: str) -> dict[str, Any]:
    """Copy only the fields the aggregate understands.

    Hook payloads carry a large, version-dependent key set; passing it through
    with ``**`` would break on the first field Hermes adds.
    """
    return {name: event[name] for name in names if name in event}


def on_pre_api(**event: Any) -> None:
    with _lock:
        agg = aggregate(event.get("session_id", ""))
        agg.note_model(event.get("model", ""), event.get("provider", ""))
        agg.touch()


def on_post_api(**event: Any) -> None:
    fields = _pick(
        event,
        "usage",
        "api_duration",
        "started_at",
        "ended_at",
        "first_chunk_at",
        "model",
        "provider",
        "response_model",
    )
    with _lock:
        aggregate(event.get("session_id", "")).on_api_success(**fields)
    _after_change(event.get("session_id", ""))


def on_api_error(**event: Any) -> None:
    fields = _pick(
        event,
        "api_duration",
        "started_at",
        "ended_at",
        "model",
        "provider",
        "error",
        "reason",
        "status_code",
    )
    with _lock:
        aggregate(event.get("session_id", "")).on_api_error(**fields)
    _after_change(event.get("session_id", ""))


def on_post_turn(**event: Any) -> None:
    with _lock:
        aggregate(event.get("session_id", "")).on_turn()
    _after_change(event.get("session_id", ""))


def on_tool_call(**event: Any) -> None:
    fields = _pick(event, "tool_name", "duration_ms", "status")
    with _lock:
        aggregate(event.get("session_id", "")).on_tool_call(**fields)
    _after_change(event.get("session_id", ""))


def on_session_boundary(started: bool, **event: Any) -> None:
    """Session start / end / finalize: refresh timestamps, persist on the way out."""
    session_id = event.get("session_id", "")
    with _lock:
        agg = aggregate(session_id)
        if started:
            agg.touch()
        snapshot = agg.snapshot()
    if not started:
        _persist(session_id, snapshot)
    _after_change(session_id)


def on_session_delete(**event: Any) -> None:
    session_id = event.get("session_id", "")
    with _lock:
        _aggregates.pop(session_id, None)
        _persisted_at.pop(session_id, None)
    if config("history_enabled"):
        store().delete(session_id)


def _after_change(session_id: str) -> None:
    """Persist (throttled) and push to the desktop bridge (throttled)."""
    snapshot = snapshot_for(session_id)
    if snapshot is None:
        return
    _persist(session_id, snapshot)
    _broadcast(snapshot)


def _persist(session_id: str, snapshot: dict[str, Any]) -> None:
    if not config("history_enabled") or not session_id:
        return
    now = time.time()
    last = _persisted_at.get(session_id, 0.0)
    # One write per 5s per session: enough for a crash-tolerant record, cheap
    # enough for a 200-call turn.
    if now - last < 5.0:
        return
    with _lock:
        _persisted_at[session_id] = now
    agg = _aggregates.get(session_id)
    if agg is not None:
        store().upsert(agg.history_record())


def _broadcast(snapshot: dict[str, Any]) -> None:
    """Fan one snapshot out to the desktop (``plugin.perf-hud.stats.update``)."""
    global _last_broadcast, _broadcasts, _event_errors
    interval = max(0.0, float(config("broadcast_min_interval_ms") or 0) / 1000.0)
    now = time.monotonic()
    if interval and now - _last_broadcast < interval:
        return
    _last_broadcast = now
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, EVENT_STATS, snapshot)
        _broadcasts += 1
    except Exception:  # bridge absent or gateway down: the popover polls instead
        _event_errors += 1


# ------------------------------------------------------------------ read side

def snapshot_for(session_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        agg = _aggregates.get(session_id or "unknown")
        return agg.snapshot() if agg else None


def focused_snapshot(session_id: str = "", fallback_latest: bool = True) -> Optional[dict[str, Any]]:
    """Snapshot for *session_id*; with no id, the most recently active session."""
    with _lock:
        if session_id:
            agg = _aggregates.get(session_id)
            return agg.snapshot() if agg else None
        if not _aggregates:
            return None
        if fallback_latest:
            agg = max(_aggregates.values(), key=lambda item: item.last_at or 0)
            return agg.snapshot()
        return None


def all_snapshots() -> list[dict[str, Any]]:
    with _lock:
        return sorted(
            (agg.snapshot() for agg in _aggregates.values()),
            key=lambda snap: snap.get("last_at") or 0,
            reverse=True,
        )


def fleet_snapshot() -> dict[str, Any]:
    with _lock:
        return aggregation.merge_snapshots(list(_aggregates.values()))


def reset(session_id: str = "") -> dict[str, Any]:
    """Reset the *current aggregation only*.

    Hermes session state, the session database and the history file are
    untouched: this drops in-memory counters for one session (or all of them
    when no id is given) and starts counting again from the next event.
    """
    with _lock:
        if session_id:
            agg = _aggregates.get(session_id)
            if agg is not None:
                agg.reset()
            return {"reset": bool(agg), "session_id": session_id}
        _aggregates.clear()
        _persisted_at.clear()
        return {"reset": True, "session_id": ""}


def history_list(limit: int = 50) -> list[dict[str, Any]]:
    return store().load()[: max(0, min(int(limit), 200))]


def clear_history() -> dict[str, Any]:
    ok = store().clear()
    return {"cleared": ok, **store().stats()}


def config_view() -> dict[str, Any]:
    view = {key: config(key) for key in DEFAULTS}
    view["history_path"] = str(store().path)
    view["history_max_sessions"] = store().max_sessions
    return view


def status() -> dict[str, Any]:
    with _lock:
        active = len(_aggregates)
        calls = sum(agg.llm_calls for agg in _aggregates.values())
    return {
        "plugin": PLUGIN_ID,
        "version": "1.0.0",
        "sessions_tracked": active,
        "llm_calls_tracked": calls,
        "broadcasts_sent": _broadcasts,
        "broadcast_errors": _event_errors,
        "history": store().stats(),
        "config": config_view(),
    }


def _reset_for_tests() -> None:
    """Test hook: clear module state without touching disk."""
    global _store, _last_broadcast, _broadcasts, _event_errors
    with _lock:
        _aggregates.clear()
        _persisted_at.clear()
    _store = None
    _last_broadcast = 0.0
    _broadcasts = 0
    _event_errors = 0
