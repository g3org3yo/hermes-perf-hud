"""Perf HUD — backend routes.

Mounted by the Hermes gateway at ``/api/plugins/perf-hud/`` and reached from the
desktop half through ``ctx.rest()``.

The live numbers already arrive over the event bridge
(``plugin.perf-hud.stats.update``); these routes exist for the three things an
event cannot do: the first paint after the popover opens, the history list, and
the two explicit controls (reset the current aggregation, clear history).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

RUNTIME_MODULE = "hermes_perf_hud_runtime"


def _runtime():
    """The same module instance the hook layer configured, or a fresh one."""
    if RUNTIME_MODULE in sys.modules:
        return sys.modules[RUNTIME_MODULE]
    path = Path(__file__).resolve().parent.parent / "runtime.py"
    spec = importlib.util.spec_from_file_location(RUNTIME_MODULE, path)
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise HTTPException(status_code=500, detail="perf-hud runtime is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_MODULE] = module
    spec.loader.exec_module(module)
    return module


@router.get("/snapshot")
def snapshot(session_id: str = Query("", description="Session to read; empty = most recently active")) -> Dict[str, Any]:
    rt = _runtime()
    snap = rt.focused_snapshot(session_id)
    return {"ok": True, "snapshot": snap, "status": rt.status()}


@router.get("/sessions")
def sessions() -> Dict[str, Any]:
    rt = _runtime()
    return {"ok": True, "sessions": rt.all_snapshots(), "fleet": rt.fleet_snapshot()}


@router.get("/history")
def history(limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
    rt = _runtime()
    return {"ok": True, "sessions": rt.history_list(limit), "store": rt.store().stats()}


@router.get("/config")
def config() -> Dict[str, Any]:
    rt = _runtime()
    return {"ok": True, "config": rt.config_view()}


@router.post("/config")
def set_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    rt = _runtime()
    key = str(payload.get("key") or "")
    if "value" not in payload:
        raise HTTPException(status_code=400, detail="value is required")
    try:
        return {"ok": True, **rt.set_config_value(key, payload["value"])}
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reset")
def reset(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reset the plugin's in-memory aggregation for one session (or all).

    Hermes session state is not touched: the session database, the transcript
    and the running agent keep their own counters. Only this plugin's view is
    zeroed.
    """
    rt = _runtime()
    session_id = str((payload or {}).get("session_id") or "")
    result = rt.reset(session_id)
    return {"ok": True, **result, "snapshot": rt.focused_snapshot(session_id)}


@router.post("/clear-history")
def clear_history() -> Dict[str, Any]:
    rt = _runtime()
    return {"ok": True, **rt.clear_history()}


@router.get("/status")
def status() -> Dict[str, Any]:
    return {"ok": True, **(_runtime().status())}
