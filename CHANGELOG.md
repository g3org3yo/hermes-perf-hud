# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-10-28

First public release. Verified against a packaged Hermes Desktop build
(`v0.22.5+`): chip, popover, history and both benchmarking controls exercised in
real sessions.

### Added

- **Status-bar chip** in `STATUSBAR_AREAS.right`, showing one of `tps`,
  `tps_ttft` or `calls` at rest, with a live value that follows the turn.
- **Statistics popover**: TPS, Session Avg TPS, TTFT (median + mean and sample
  count), LLM calls, turns, input/output/cache-read/cache-write/reasoning tokens,
  cache hit rate, tool time and its share, per-tool breakdown, model, provider,
  session duration and API errors.
- **`TPS` (core-compatible)**: `Σ output_tokens / Σ api_duration` over the last
  10 completed API calls — the same window and arithmetic as
  `agent/turn_usage.py` and the CLI status bar.
- **`Session Avg TPS`**: the same ratio over every call in the session, shown
  under its own label and never as "TPS".
- **TTFT** from `first_chunk_at − started_at`, median of real samples only.
- **Local history** (on by default): one JSON file per profile, capped at 50
  sessions, containing performance metadata only — model, provider, timestamps,
  duration, TTFT, core TPS, token counts, cache hit rate, tool time, LLM calls.
  Written atomically through an allow-list; a corrupt file is quarantined instead
  of crashing the host.
- **Benchmarking controls**: *Reset current statistics* (plugin aggregation only —
  Hermes session state untouched) and *Clear history*.
- **Live push** to the desktop over the public
  `hermes_cli.plugin_events.broadcast_plugin_event` bridge, throttled, with a
  REST polling fallback (`GET /api/plugins/perf-hud/snapshot`).
- **REST surface**: `/snapshot`, `/sessions`, `/history`, `/config` (GET/POST),
  `/reset`, `/clear-history`, `/status`.
- **Configuration** through the plugin's own `plugins.entries.perf-hud.settings.*`
  subtree: `chip_enabled`, `chip_metric`, `history_enabled`,
  `history_max_sessions`, `poll_ms`, `broadcast_min_interval_ms`.
- **Documentation**: `README.md`, `docs/ARCHITECTURE.md`, `docs/METRICS.md`.
- **Tests**: 65 tests on stdlib `unittest`, covering the pure aggregation layer,
  the history store, the runtime (including bridge-failure isolation and the
  "reset must not touch Hermes state" contract) and the FastAPI router.

### Notes

- Hermes core is **not** modified. The plugin uses documented seams only:
  `ctx.register_hook`, `ctx.get_config` / `ctx.set_config`, `ctx.state.data_dir`,
  `broadcast_plugin_event`, the plugin `APIRouter` mount, and the Desktop
  `@hermes/plugin-sdk`.
- All nine observer hooks are read-only: no handler returns a value, blocks a
  tool, or mutates a message. Disabling the plugin cannot change how a turn runs.
- No network requests are made by this plugin. The only outbound path is the
  in-process desktop bridge; the REST routes are served by the local dashboard.
- Requires Hermes `>=0.22.5` (the first release whose desktop SDK exposes
  `STATUSBAR_AREAS`).
- The desktop half is materialised into `<HERMES_HOME>/desktop-plugins/` by the
  Electron app at startup, so the chip appears after one restart.
