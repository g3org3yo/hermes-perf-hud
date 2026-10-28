"""Hook integration, event bridge and control-surface contracts."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from _loader import ROOT, load

rt = load("runtime")


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeCtx:
    """Minimal stand-in for the plugin context: config + storage only."""

    def __init__(self, data_dir: Path, settings=None) -> None:
        self.state = FakeState(data_dir)
        self.settings = dict(settings or {})
        self.hooks: list[tuple[str, object]] = []
        self.registered: dict[str, object] = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def register(self, item):  # pragma: no cover - backend half registers none
        self.registered[str(item)] = item


class Bridge:
    """Captures what the plugin would push to the desktop."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.module = types.ModuleType("hermes_cli.plugin_events")

        def broadcast(plugin_id, event, payload=None):
            self.events.append((plugin_id, event, payload or {}))

        self.module.broadcast_plugin_event = broadcast
        self.install()

    def install(self) -> None:
        package = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
        package.plugin_events = self.module
        sys.modules["hermes_cli"] = package
        sys.modules["hermes_cli.plugin_events"] = self.module

    @staticmethod
    def uninstall() -> None:
        sys.modules.pop("hermes_cli.plugin_events", None)
        sys.modules.pop("hermes_cli", None)


def api_event(session_id="s1", out=100, duration=2.0, **extra):
    payload = {
        "session_id": session_id,
        "turn_id": "t1",
        "api_call_count": 1,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_duration": duration,
        "started_at": 1000.0,
        "ended_at": 1000.0 + duration,
        "first_chunk_at": 1000.4,
        "finish_reason": "stop",
        "usage": {"output_tokens": out, "input_tokens": 500, "cache_read_tokens": 1500},
        # Fields Hermes adds that the aggregate does not model:
        "request": {"messages": [{"role": "user", "content": "SECRET PROMPT"}]},
        "response": {"content": "SECRET RESPONSE"},
        "assistant_message": object(),
        "something_new_in_a_future_version": 1,
    }
    payload.update(extra)
    return payload


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        rt._reset_for_tests()
        self.bridge = Bridge()
        self.addCleanup(Bridge.uninstall)
        self.ctx = FakeCtx(self.data_dir)
        rt.configure(self.ctx)

    def history_rows(self):
        path = self.data_dir / "history.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["sessions"]

    # ------------------------------------------------------------- recording

    def test_post_api_records_only_known_fields(self):
        rt.on_post_api(**api_event())
        snap = rt.focused_snapshot("s1")
        self.assertEqual(snap["llm_calls"], 1)
        self.assertEqual(snap["output_tokens"], 100)
        self.assertAlmostEqual(snap["tps_core"], 50.0, places=3)
        self.assertEqual(snap["ttft_samples"], 1)
        self.assertAlmostEqual(snap["ttft_ms"], 400, delta=1)
        # The payload carried a prompt and a response: neither may be retained.
        blob = json.dumps(snap)
        self.assertNotIn("SECRET", blob)

    def test_unknown_payload_keys_never_break_the_handler(self):
        rt.on_post_api(**api_event(brand_new_field="x", another={"nested": True}))
        self.assertEqual(rt.focused_snapshot("s1")["llm_calls"], 1)

    def test_core_tps_matches_cli_definition(self):
        rt.on_post_api(**api_event(out=100, duration=2.0, ended_at=1002.0))
        rt.on_post_api(**api_event(out=300, duration=3.0, ended_at=1003.0, started_at=1000.0))
        # sum(output) / sum(duration) = 400 / 5
        self.assertAlmostEqual(rt.focused_snapshot("s1")["tps_core"], 80.0, places=3)

    def test_turns_tools_and_errors_are_separate_counters(self):
        rt.on_post_turn(session_id="s1", message_count=3)
        rt.on_tool_call(session_id="s1", tool_name="terminal", duration_ms=1200, status="ok")
        rt.on_api_error(session_id="s1", api_duration=0.5, error={"type": "Timeout"}, status_code=504)
        snap = rt.focused_snapshot("s1")
        self.assertEqual(snap["turns"], 1)
        self.assertEqual(snap["tool_calls"], 1)
        self.assertEqual(snap["tool_time_ms"], 1200)
        self.assertEqual(snap["llm_calls"], 1)
        self.assertEqual(snap["api_errors"], 1)

    def test_sessions_are_isolated(self):
        rt.on_post_api(**api_event("a", out=10, duration=1.0))
        rt.on_post_api(**api_event("b", out=90, duration=1.0))
        self.assertEqual(len(rt.all_snapshots()), 2)
        self.assertEqual(rt.focused_snapshot("b")["output_tokens"], 90)
        self.assertEqual(rt.fleet_snapshot()["output_tokens"], 100)

    def test_session_finalize_persists_history(self):
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1", reason="complete")
        rows = self.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "s1")
        self.assertEqual(rows[0]["model"], "deepseek-chat")
        self.assertNotIn("messages", rows[0])

    def test_history_disabled_writes_nothing(self):
        rt._reset_for_tests()
        rt.configure(FakeCtx(self.data_dir, {"history_enabled": False}))
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1")
        self.assertEqual(self.history_rows(), [])

    def test_session_delete_purges_its_record(self):
        rt.on_session_boundary(True, session_id="s1")
        rt.on_session_boundary(False, session_id="s1")
        self.assertEqual(len(self.history_rows()), 1)
        rt.on_session_delete(session_id="s1", reason="user")
        self.assertEqual(self.history_rows(), [])
        self.assertIsNone(rt.focused_snapshot("s1"))

    # ---------------------------------------------------------------- bridge

    def test_every_change_is_pushed_to_the_desktop(self):
        rt.on_post_api(**api_event())
        self.assertTrue(self.bridge.events)
        plugin_id, event, payload = self.bridge.events[-1]
        self.assertEqual(plugin_id, "perf-hud")
        self.assertEqual(event, "stats.update")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["llm_calls"], 1)

    def test_pushes_are_throttled(self):
        for _ in range(5):
            rt.on_post_api(**api_event(out=10, duration=1.0))
        self.assertEqual(len(self.bridge.events), 1)
        self.assertGreaterEqual(rt.status()["llm_calls_tracked"], 5)

    def test_bridge_failure_is_survivable(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("gateway down")

        self.bridge.module.broadcast_plugin_event = boom
        rt.on_post_api(**api_event())
        self.assertEqual(rt.status()["broadcast_errors"], 1)
        self.assertEqual(rt.focused_snapshot("s1")["llm_calls"], 1)

    # -------------------------------------------------------------- controls

    def test_reset_clears_aggregation_but_not_history(self):
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1")
        self.assertEqual(len(self.history_rows()), 1)

        rt.reset("s1")
        snap = rt.focused_snapshot("s1")
        self.assertEqual(snap["llm_calls"], 0)
        self.assertIsNone(snap["tps_core"])
        self.assertEqual(snap["output_tokens"], 0)
        # The stored history record survives: reset is not "forget the session".
        self.assertEqual(len(self.history_rows()), 1)

    def test_reset_all_clears_every_session_view(self):
        rt.on_post_api(**api_event("a"))
        rt.on_post_api(**api_event("b"))
        rt.reset("")
        self.assertEqual(rt.all_snapshots(), [])

    def test_clear_history_keeps_live_numbers(self):
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1")
        rt.clear_history()
        self.assertEqual(self.history_rows(), [])
        self.assertEqual(rt.focused_snapshot("s1")["llm_calls"], 1)

    def test_reset_does_not_touch_hermes_state(self):
        """The reset path must not import or call anything that writes sessions."""
        rt.on_post_api(**api_event())
        before = {name for name in sys.modules if name.startswith("hermes_state")}
        rt.reset("s1")
        after = {name for name in sys.modules if name.startswith("hermes_state")}
        self.assertEqual(after - before, set())  # no new session-store machinery
        self.assertEqual(self.history_rows()[0]["llm_calls"], 1)

    def test_config_write_goes_through_the_context(self):
        rt.set_config_value("chip_metric", "calls")
        self.assertEqual(self.ctx.settings["chip_metric"], "calls")
        self.assertEqual(rt.config_view()["chip_metric"], "calls")

    def test_config_rejects_unknown_key_and_wrong_type(self):
        with self.assertRaises(KeyError):
            rt.set_config_value("nope", 1)
        with self.assertRaises(ValueError):
            rt.set_config_value("chip_metric", 5)

    def test_status_surface(self):
        rt.on_post_api(**api_event())
        status = rt.status()
        self.assertEqual(status["plugin"], "perf-hud")
        self.assertEqual(status["sessions_tracked"], 1)
        self.assertIn("history", status)


class HookRegistrationTests(unittest.TestCase):
    EXPECTED = {
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "post_llm_call",
        "post_tool_call",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_delete",
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        rt._reset_for_tests()
        self.bridge = Bridge()
        self.addCleanup(Bridge.uninstall)
        spec = importlib.util.spec_from_file_location("hermes_perf_hud_init", ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_perf_hud_init"] = module
        spec.loader.exec_module(module)
        self.plugin = module

    def test_register_wires_exactly_the_declared_hooks(self):
        ctx = FakeCtx(Path(self._tmp.name))
        self.plugin.register(ctx)
        self.assertEqual({name for name, _ in ctx.hooks}, self.EXPECTED)
        self.assertEqual(ctx.registered, {})  # backend half adds no tools or routes

    def test_handlers_swallow_plugin_bugs(self):
        ctx = FakeCtx(Path(self._tmp.name))
        self.plugin.register(ctx)
        hooks = dict(ctx.hooks)
        # A hook call with a payload the aggregate cannot use must not raise.
        hooks["post_api_request"](usage="not-a-dict", api_duration=None, session_id="s1")
        self.assertEqual(rt.focused_snapshot("s1")["llm_calls"], 1)

    def test_session_start_and_end_reach_the_runtime(self):
        ctx = FakeCtx(Path(self._tmp.name))
        self.plugin.register(ctx)
        hooks = dict(ctx.hooks)
        hooks["on_session_start"](session_id="s9")
        hooks["post_api_request"](**api_event("s9"))
        hooks["on_session_end"](session_id="s9")
        self.assertEqual(rt.focused_snapshot("s9")["llm_calls"], 1)
        path = Path(self._tmp.name) / "history.json"
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
