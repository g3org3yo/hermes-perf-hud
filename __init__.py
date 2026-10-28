"""Perf HUD -- live throughput, TTFT, tokens and tool time for Hermes.

Backend half of the plugin. It observes the public observer hooks, keeps the
numbers in memory (``runtime.py``), persists an allow-listed performance record
per session (``history.py``), and pushes snapshots to the desktop browser half
over ``plugin.perf-hud.stats.update``.

Design rules that keep this a plugin rather than a core change:

* read-only observation -- no hook here returns a value, blocks a tool, or
  transforms a message, so turning the plugin off changes nothing about how a
  turn runs;
* no core imports beyond ``hermes_cli.plugin_events`` (the documented bridge),
  and that one is optional;
* no prompt, response, tool argument or tool result ever leaves a handler --
  only counters, timings and token counts.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

PLUGIN_ID = "perf-hud"
RUNTIME_MODULE = "hermes_perf_hud_runtime"

logger = logging.getLogger(__name__)


def _runtime():
    """Load ``runtime.py`` once, shared with the dashboard API half."""
    if RUNTIME_MODULE in sys.modules:
        return sys.modules[RUNTIME_MODULE]
    path = Path(__file__).resolve().parent / "runtime.py"
    spec = importlib.util.spec_from_file_location(RUNTIME_MODULE, path)
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise ImportError("perf-hud: runtime.py is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _guard(name: str, fn: Callable[..., Any]) -> Callable[..., None]:
    """Never raise into the host: a telemetry bug must not break a turn."""

    def handler(**event: Any) -> None:
        try:
            fn(**event)
        except Exception:  # pragma: no cover - defensive, logged not raised
            logger.debug("perf-hud: %s handler failed", name, exc_info=True)

    handler.__name__ = f"perf_hud_{name}"
    return handler


def register(ctx) -> None:
    rt = _runtime()
    rt.configure(ctx)

    def session_start(**event: Any) -> None:
        rt.on_session_boundary(True, **event)

    def session_end(**event: Any) -> None:
        rt.on_session_boundary(False, **event)

    hooks: dict[str, Callable[..., Any]] = {
        # request-scoped: one entry per provider attempt (the TPS/TTFT source)
        "pre_api_request": rt.on_pre_api,
        "post_api_request": rt.on_post_api,
        "api_request_error": rt.on_api_error,
        # turn + tool lifecycle
        "post_llm_call": rt.on_post_turn,
        "post_tool_call": rt.on_tool_call,
        # session lifecycle: timestamps, history flush, delete purge
        "on_session_start": session_start,
        "on_session_end": session_end,
        "on_session_finalize": session_end,
        "on_session_delete": rt.on_session_delete,
    }
    for name, fn in hooks.items():
        ctx.register_hook(name, _guard(name, fn))
