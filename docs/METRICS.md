# Metrics

Every number Perf HUD shows, with the exact formula the code uses. Threshold of
honesty first: two of these look alike and are not — `TPS` and `Session Avg TPS`
are different metrics, and the UI never mixes them.

Source of truth: `aggregation.py` (`SessionAggregate`) and the snapshot it emits.
The Desktop half only formats what it receives.

## TPS (core-compatible)

Hermes' own throughput gauge is a *windowed* ratio, not an average of per-call
speeds. Perf HUD reproduces it exactly:

```
             Σ output_tokens      over the last 10 completed API calls
TPS  =  ─────────────────────────
             Σ api_duration       over the same 10 calls
```

* Window: `CORE_WINDOW = 10`, matching `agent/turn_usage.py`
  (`_api_latency_history` and `_api_output_history`, both `deque(maxlen=10)`)
  and the CLI status bar.
* `api_duration` is the provider call span in seconds, as Hermes reports it.
* A failed call increments `llm_calls` and adds its latency to `total_api_ms`,
  but is **not** appended to the 10-call window: an error carries no tokens, and
  entering it as `(0 tokens, N seconds)` would drag the window down. It does
  therefore count against `Session Avg TPS`, whose denominator is every call.
* Fewer than one sample → `null`, rendered as `—`.

The snapshot also carries `tps_core_window` and `tps_core_window_max` so the
tooltip can say *"last 7 of 10 calls"* instead of pretending the window is full.

**Sum over sum, not the mean of means.** Ten calls that produce 400 tokens in
5.0 s are 80 t/s. Ten per-call rates averaging 75 t/s would be wrong here
because the slow calls carried more tokens. A unit test pins this
(`test_core_tps_is_sum_over_sum`).

## Session Avg TPS

```
                          Σ output_tokens        over every call in the session
Session Avg TPS  =  ────────────────────────────
                          Σ api_duration / 1000        (seconds)
```

Label is always the full `Session Avg TPS`. It answers "what did this session
average", which is a useful benchmark and a misleading saturation number — hence
the separate label and the separate field (`session_avg_tps`), never rendered as
"TPS".

## TTFT

```
ttft_sample = first_chunk_at − started_at        (per API call, ms)
ttft_ms     = p50(all samples)                   (median)
ttft_ms_avg = mean(all samples)
```

Median is the headline because one cold start should not move it; the mean and
`ttft_samples` ride along in the snapshot. A call whose payload has no
`first_chunk_at` produces **no sample** — it is not counted as zero, and the
field reads `—` until a real sample exists.

## Tokens

Straight sums of what the provider reported, per session:

| Field | Meaning |
| --- | --- |
| `input_tokens` | fresh (uncached) input tokens |
| `output_tokens` | generated tokens |
| `cache_read_tokens` | input tokens served from cache |
| `cache_write_tokens` | input tokens written to cache |
| `reasoning_tokens` | reasoning/thinking tokens, when the provider reports them |

Numbers arriving as strings are coerced; garbage becomes `0` rather than raising.

## Cache hit rate

```
                 cache_read_tokens
cache_hit_pct = ────────────────────────────────────── × 100
                 cache_read_tokens + input_tokens
```

This is Hermes' bucket definition: `cache_read` over `cache_read` plus fresh
input. It is a ratio of reported token buckets — not a billing statement, and not
comparable across providers that count cached tokens differently. No tokens →
`null` (not 0%).

## LLM calls

One increment per completed provider request — success (`post_api_request`) or
failure (`api_request_error`). This is the authoritative call count, and it is
what the UI shows as **LLM calls**.

## Agent steps*

`agent_steps` exists in the snapshot only for UI compatibility, and it is exactly
`llm_calls`. The current Hermes telemetry API exposes no independent agent-step
counter, so the UI always renders it as `Agent steps*` with the footnote:

> \*Equivalent to LLM calls in the current Hermes telemetry API.

The footnote string is a constant (`AGENT_STEPS_NOTE`) that travels inside the
snapshot (`agent_steps_note`), so no client can display the alias without it.

## Turns

`post_llm_call` increments `turns`. A turn is one completed LLM call cycle as
Hermes reports it; it is a separate counter from `llm_calls` because one turn can
involve retries or multiple provider attempts.

## Tool time

```
tool_time_ms        = Σ duration_ms   over every post_tool_call
tool_time_share_pct = 100 × tool_time_ms / (tool_time_ms + Σ api_duration)
```

Per-tool totals (`tools`) and `tool_errors` are kept as well. The share is of
*measured* time — model calls plus tools — not of wall-clock session time, so
the percentage stays meaningful when the user is reading the popover instead of
driving the agent.

## Session duration

```
duration_ms = (last_at − first_at) × 1000        wall clock
```

`first_at` is stamped by the first observed event for the session and `last_at`
by the most recent one.

This is deliberately **not** `ended_at − started_at` from a hook payload: those
two values describe a single API call, so using them made the session look
shorter than the tool time it contained (tools run *between* calls). API
timestamps are used only for TTFT. Two tests pin the behaviour:
`test_duration_is_wall_clock_not_api_span` and `test_duration_grows_with_activity`.

## Model and provider

Taken from the last `pre_api_request` / `post_api_request` payload (`model`,
`provider`, falling back to `response_model`). Empty until the first call
completes.

## Fleet view (`/sessions`, popover header)

`merge_snapshots()` sums counters across live sessions and re-derives rates from
the summed components. It never invents a fleet "core TPS" by averaging
per-session windows — `tps_core` is `null` at fleet level, because there is no
single 10-call window that spans sessions.

## Rounded where it lands

`tps_core` and `session_avg_tps` to 1 decimal, `ttft_*` and durations to whole ms,
`cache_hit_pct` and `tool_time_share_pct` to whole percent, tokens as integers.
Rounding happens once, in `snapshot()`, so REST, the event bridge and the history
record all carry identical numbers.
