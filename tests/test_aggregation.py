"""Behaviour contracts for the pure aggregation layer."""

from __future__ import annotations

import unittest

from _loader import load

agg = load("aggregation")


def call(agg_obj, out_tokens, duration, **kwargs):
    agg_obj.on_api_success(
        usage={"output_tokens": out_tokens, "input_tokens": kwargs.pop("input_tokens", 100)},
        api_duration=duration,
        **kwargs,
    )


class CoreWindowTests(unittest.TestCase):
    def test_empty_session_has_no_tps(self):
        session = agg.SessionAggregate("s1")
        snap = session.snapshot()
        self.assertIsNone(snap["tps_core"])
        self.assertIsNone(snap["session_avg_tps"])
        self.assertEqual(snap["llm_calls"], 0)

    def test_core_tps_is_sum_over_sum(self):
        session = agg.SessionAggregate("s1")
        call(session, 100, 2.0)  # 50 t/s
        call(session, 300, 3.0)  # 100 t/s
        # sum/sum = 400/5 = 80, NOT the mean of 50 and 100
        self.assertAlmostEqual(session.tps_core(), 80.0, places=3)

    def test_core_window_drops_call_eleven(self):
        session = agg.SessionAggregate("s1")
        call(session, 1000, 1.0)  # fast, falls out of the window
        for _ in range(10):
            call(session, 100, 1.0)  # 100 t/s each
        # window holds the last 10 calls only -> 1000/10
        self.assertAlmostEqual(session.tps_core(), 100.0, places=3)
        self.assertEqual(session.snapshot()["tps_core_window"], agg.CORE_WINDOW)

    def test_session_avg_uses_every_call(self):
        session = agg.SessionAggregate("s1")
        call(session, 1000, 1.0)  # 1000 t/s, then pushed out of the core window
        for _ in range(10):
            call(session, 100, 1.0)
        self.assertAlmostEqual(session.tps_core(), 100.0, places=3)
        # 2000 tokens over 11 s
        self.assertAlmostEqual(session.session_avg_tps(), 2000 / 11, places=3)
        self.assertNotAlmostEqual(session.tps_core(), session.session_avg_tps(), places=1)

    def test_zero_duration_never_divides_by_zero(self):
        session = agg.SessionAggregate("s1")
        call(session, 10, 0.0)
        self.assertIsNone(session.tps_core())
        self.assertEqual(session.snapshot()["llm_calls"], 1)

    def test_tool_call_only_response_counts_as_a_call(self):
        session = agg.SessionAggregate("s1")
        call(session, 0, 2.0)
        self.assertEqual(session.snapshot()["llm_calls"], 1)
        self.assertAlmostEqual(session.tps_core(), 0.0, places=3)


class MetricLabelTests(unittest.TestCase):
    def test_agent_steps_mirrors_llm_calls_with_footnote(self):
        session = agg.SessionAggregate("s1")
        call(session, 10, 1.0)
        session.on_turn()
        snap = session.snapshot()
        self.assertEqual(snap["agent_steps"], snap["llm_calls"])
        self.assertIn("Equivalent to LLM calls", snap["agent_steps_note"])
        self.assertIn("Equivalent to LLM calls", agg.AGENT_STEPS_NOTE)

    def test_core_window_is_ten(self):
        self.assertEqual(agg.CORE_WINDOW, 10)


class TokenAndTimingTests(unittest.TestCase):
    def test_ttft_median_and_samples(self):
        session = agg.SessionAggregate("s1")
        for ttft, duration in ((0.2, 2.0), (0.4, 2.0), (0.9, 2.0)):
            session.on_api_success(
                usage={"output_tokens": 50},
                api_duration=duration,
                started_at=100.0,
                first_chunk_at=100.0 + ttft,
            )
        snap = session.snapshot()
        self.assertEqual(snap["ttft_samples"], 3)
        self.assertAlmostEqual(snap["ttft_ms"], 400, delta=1)
        self.assertAlmostEqual(snap["ttft_ms_avg"], 500, delta=1)

    def test_missing_ttft_is_not_a_sample(self):
        session = agg.SessionAggregate("s1")
        call(session, 10, 1.0, started_at=50.0)
        self.assertIsNone(session.snapshot()["ttft_ms"])
        self.assertEqual(session.snapshot()["ttft_samples"], 0)

    def test_cache_hit_rate_uses_read_over_read_plus_fresh(self):
        session = agg.SessionAggregate("s1")
        session.on_api_success(
            usage={"output_tokens": 10, "input_tokens": 25, "cache_read_tokens": 75}, api_duration=1.0
        )
        self.assertAlmostEqual(session.cache_hit_pct(), 75.0, places=3)

    def test_no_token_usage_means_no_cache_rate(self):
        session = agg.SessionAggregate("s1")
        call(session, 0, 1.0, input_tokens=0)
        self.assertIsNone(session.cache_hit_pct())


class ToolTests(unittest.TestCase):
    def test_tool_time_accumulates_per_name(self):
        session = agg.SessionAggregate("s1")
        session.on_tool_call(tool_name="terminal", duration_ms=1200, status="ok")
        session.on_tool_call(tool_name="terminal", duration_ms=800, status="ok")
        session.on_tool_call(tool_name="read_file", duration_ms=100, status="error")
        snap = session.snapshot()
        self.assertEqual(snap["tool_calls"], 3)
        self.assertEqual(snap["tool_errors"], 1)
        self.assertEqual(snap["tool_time_ms"], 2100)
        self.assertEqual(snap["tools"][0]["name"], "terminal")
        self.assertEqual(snap["tools"][0]["calls"], 2)

    def test_tool_share_is_bounded(self):
        session = agg.SessionAggregate("s1")
        call(session, 100, 1.0)
        session.on_tool_call(tool_name="terminal", duration_ms=1000)
        share = session.snapshot()["tool_time_share_pct"]
        self.assertGreater(share, 0)
        self.assertLess(share, 100)


class DurationTests(unittest.TestCase):
    def test_duration_is_wall_clock_not_api_span(self):
        """A call reported as started a minute ago must not stretch the session."""
        import time as _time

        session = agg.SessionAggregate("s1")
        session.on_api_success(
            usage={"output_tokens": 10}, api_duration=1.0, started_at=_time.time() - 60, ended_at=_time.time() - 59
        )
        snap = session.snapshot()
        self.assertIsNotNone(snap["duration_ms"])
        self.assertLess(snap["duration_ms"], 1000)

    def test_duration_grows_with_activity(self):
        import time as _time

        session = agg.SessionAggregate("s1")
        session.on_api_success(usage={"output_tokens": 1}, api_duration=0.1)
        _time.sleep(0.05)
        session.on_turn()
        self.assertGreaterEqual(session.snapshot()["duration_ms"], 40)


class ErrorTests(unittest.TestCase):
    def test_error_counts_as_call_and_keeps_only_classifier(self):
        session = agg.SessionAggregate("s1")
        session.on_api_error(
            api_duration=1.5,
            error={"type": "RateLimitError", "message": "429 from provider"},
            status_code=429,
            reason="retryable",
        )
        snap = session.snapshot()
        self.assertEqual(snap["llm_calls"], 1)
        self.assertEqual(snap["api_errors"], 1)
        self.assertEqual(snap["last_error"]["kind"], "RateLimitError")
        self.assertEqual(snap["last_error"]["status"], 429)
        # Long messages are truncated, never carried whole.
        self.assertLessEqual(len(snap["last_error"]["message"]), 160)


class PrivacyTests(unittest.TestCase):
    def test_history_record_is_an_allow_list(self):
        session = agg.SessionAggregate("s1")
        call(session, 100, 1.0, model="test-model", provider="test-provider")
        record = session.history_record()
        self.assertEqual(set(record), set(agg.SessionAggregate("x").history_record()) | {"session_id"})
        for forbidden in ("prompt", "messages", "response", "tool_args", "args", "result", "content", "assistant_message"):
            self.assertNotIn(forbidden, record)
        self.assertEqual(record["model"], "test-model")
        self.assertEqual(record["provider"], "test-provider")
        self.assertEqual(record["llm_calls"], 1)

    def test_snapshot_carries_no_payload_fields(self):
        session = agg.SessionAggregate("s1")
        call(session, 100, 1.0)
        snap = session.snapshot()
        for forbidden in ("prompt", "messages", "response", "request", "args", "result", "content"):
            self.assertNotIn(forbidden, snap)


class ResetTests(unittest.TestCase):
    def test_reset_clears_only_that_session(self):
        one = agg.SessionAggregate("s1")
        two = agg.SessionAggregate("s2")
        call(one, 100, 1.0)
        call(two, 200, 1.0)
        one.reset()
        self.assertEqual(one.snapshot()["llm_calls"], 0)
        self.assertIsNone(one.snapshot()["tps_core"])
        self.assertEqual(two.snapshot()["llm_calls"], 1)
        self.assertEqual(one.session_id, "s1")


class MergeTests(unittest.TestCase):
    def test_fleet_view_sums_and_never_invents_core_tps(self):
        one = agg.SessionAggregate("s1")
        two = agg.SessionAggregate("s2")
        call(one, 100, 1.0)
        call(two, 300, 1.0)
        merged = agg.merge_snapshots([one, two])
        self.assertEqual(merged["sessions"], 2)
        self.assertEqual(merged["llm_calls"], 2)
        self.assertEqual(merged["output_tokens"], 400)
        self.assertIsNone(merged["tps_core"])
        self.assertAlmostEqual(merged["session_avg_tps"], 200.0, places=1)


class RobustnessTests(unittest.TestCase):
    def test_string_numbers_are_tolerated(self):
        session = agg.SessionAggregate("s1")
        session.on_api_success(usage={"output_tokens": "120"}, api_duration="2")
        self.assertAlmostEqual(session.tps_core(), 60.0, places=3)

    def test_garbage_usage_does_not_raise(self):
        session = agg.SessionAggregate("s1")
        session.on_api_success(usage={"output_tokens": None, "input_tokens": "n/a"}, api_duration="x")
        self.assertEqual(session.snapshot()["llm_calls"], 1)


if __name__ == "__main__":
    unittest.main()
