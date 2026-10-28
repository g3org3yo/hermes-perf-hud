# Architecture

How Perf HUD gets from a provider response to a number in the status bar, and
what restricts it at each step. Everything below describes the shipped code in
this repository — not a target design.

```
agent turn
   │
   │  observer hooks (public seam, no return values)
   ▼
__init__.py  ──  register(ctx) + _guard()
   │              payload filtered by runtime._pick()
   ▼
runtime.py  ──  one live registry (module hermes_perf_hud_runtime)
   │
   ├── aggregation.py   pure: SessionAggregate, no I/O, no host imports
   ├── history.py       allow-listed JSON records, atomic replace
   │
   ├── broadcast_plugin_event("perf-hud", "stats.update", snapshot)
   │        │ (guarded import; absent bridge = polling fallback)
   │        ▼
   │   Desktop renderer: onEvent → chip + popover
   │
   └── FastAPI router  /api/plugins/perf-hud/*
            GET  /snapshot   first paint, polling fallback
            GET  /sessions   all live sessions
            GET  /history    persisted records
            GET  /config     current settings
            POST /config     write one setting
            POST /reset      drop in-memory counters
            POST /clear-history
            GET  /status     counters, history stats
```

## 1. Observer hooks

`__init__.py` registers nine hooks through `ctx.register_hook(name, handler)`:

| Hook | Handler | Purpose |
| --- | --- | --- |
| `pre_api_request` | `runtime.on_pre_api` | model/provider identity, session heartbeat |
| `post_api_request` | `runtime.on_post_api` | the measurement point: usage, `api_duration`, `first_chunk_at` |
| `api_request_error` | `runtime.on_api_error` | failed attempts (counted, never rate-limited away) |
| `post_llm_call` | `runtime.on_post_turn` | turn counter |
| `post_tool_call` | `runtime.on_tool_call` | tool duration and status |
| `on_session_start` | `session_start` | timestamp the session |
| `on_session_end`, `on_session_finalize` | `session_end` | flush the history record |
| `on_session_delete` | `runtime.on_session_delete` | purge that session's record |

All nine are *observers*. No handler returns a value, none can block a tool, and
none mutates a message — with the plugin disabled a turn behaves identically.
`plugin.yaml` declares exactly this list, and `hermes plugins validate` compares
the declaration against the registrations.

## 2. Hook payload filtering

Hook payloads are wide and grow between Hermes versions. Passing `**event` into
the aggregate would break the plugin the first time a field is added or renamed,
so every handler copies an explicit allow-list with `runtime._pick(event, ...)`:

```python
fields = _pick(event, "usage", "api_duration", "started_at", "ended_at",
               "first_chunk_at", "model", "provider", "response_model")
```

Unknown keys are dropped; missing keys are simply absent, and the aggregate
treats `None` as "no sample". This is why the plugin keeps working on hosts whose
telemetry payload differs in detail.

## 3. Fail-open handlers

Every registered handler is wrapped by `__init__.py::_guard`, which catches
`Exception` and logs at debug level. A bug in the aggregation layer can therefore
cost you a number in the HUD; it cannot fail a turn, abort a tool, or surface an
error to the agent loop. The history store additionally quarantines a corrupt
JSON file instead of raising (`history.py`).

## 4. Aggregation

`aggregation.py` is the only place that does arithmetic, and it is pure: events
in, snapshot out. No filesystem, no network, no host imports, one clock
(`time.time()`). Consequences:

* the metric behaviour is unit-testable without an engine or a session;
* the dashboard half recomputes nothing — it renders the snapshot it is given;
* anything that needs a new metric needs an event field and a formula, not a new
  data path.

Metric definitions live in [METRICS.md](./METRICS.md).

## 5. Runtime registry

`runtime.py` holds the process-wide state: a `dict[session_id, SessionAggregate]`,
one `HistoryStore`, the effective config getter, and broadcast counters. It is
protected by a single `threading.RLock`, because hook handlers can be invoked
from different threads than the dashboard request handler.

The plugin directory is named `perf-hud`, so it is not importable as a package.
Both halves therefore load `runtime.py` **by path under the fixed module name**
`hermes_perf_hud_runtime`:

```python
if RUNTIME_MODULE in sys.modules:      # one instance, or the two halves split
    return sys.modules[RUNTIME_MODULE]
...
sys.modules[RUNTIME_MODULE] = module
```

Without that, `__init__.py` and `dashboard/plugin_api.py` would each build their
own registry: the chip would show data while the popover showed nothing.

`configure(ctx)` binds three documented seams at registration time — `ctx.get_config`,
`ctx.set_config`, `ctx.state.data_dir` — and falls back to `<plugin>/data/` when
the host exposes no plugin state.

## 6. Python → Desktop events

After every state change, `_after_change()` does two things:

1. **persist** (throttled to one write per session per 5 s — enough to survive a
   crash, cheap enough for a 200-call turn), and
2. **broadcast** a snapshot to the desktop:

```python
from hermes_cli.plugin_events import broadcast_plugin_event
broadcast_plugin_event("perf-hud", "stats.update", snapshot)
```

The import sits inside a `try/except`: on a host without the bridge the plugin
still aggregates, still persists history, and the popover falls back to polling
`GET /snapshot`. Broadcasts are throttled by `broadcast_min_interval_ms`
(default 400 ms ≈ 2.5 pushes/s), so a fast model cannot flood the renderer with
IPC. `status()` exposes `broadcasts_sent` / `broadcast_errors` so the bridge can
be diagnosed from outside.

## 7. Desktop surface

`desktop/plugin.js` is ESM that imports **only** from `@hermes/plugin-sdk`
(`STATUSBAR_AREAS`, `Popover`, `Separator`, `Button`, `cn`, `host`), `react`, and
`react/jsx-runtime`. It renders with raw `jsx()` calls and no build step.

* the chip is registered into `STATUSBAR_AREAS.right` and shows one metric at
  rest (`chip_metric`: `tps`, `tps_ttft`, `calls`);
* the popover reads `onEvent("stats.update")` while open and polls `/snapshot`
  as a fallback;
* the controls call `POST /reset` and `POST /clear-history` through the plugin's
  own `rest` helper — the same routes any other client would use.

No DOM is patched, no internal component is reached into, and nothing is styled
outside the host's own primitives.

## 8. History persistence

`history.py` keeps a single JSON file per Hermes profile
(`<plugin state dir>/history.json`), shaped:

```json
{"version": 1, "max_sessions": 50, "sessions": [ ... ]}
```

Writes go through an allow-list constant, `RECORD_FIELDS`, which is the only
place a field name can enter the file. Adding a metric to the snapshot does not
add it to disk; someone must edit that tuple deliberately. Records are trimmed to
`max_sessions` (50) newest-first, written via temp file + `os.replace`, and a file
that fails to parse is renamed to `history.json.corrupt-<stamp>` rather than
crashing the host.

## 9. Session identity

Sessions are keyed by the `session_id` present in the hook payload — the Hermes
session UUID. An event without one is aggregated under the sentinel `"unknown"`
and is never persisted (persistence requires a real id). `snapshot` with no
`session_id` returns the most recently active session, which is what the popover
wants when the user is looking at the current chat.

## 10. Process and isolation behaviour

The registry is in-process. Under the desktop backend (`hermes serve`) every
window shares one process, so the chip and popover agree.

If `dashboard.turn_isolation` runs turns in a compute-host child process, that
child has its own registry:

* live events still reach the renderer (the bridge fans out from the child);
* `GET /snapshot` in the parent will not see the child's counters;
* `GET /history` stays complete, because the file is on shared disk.

This deployment runs `plugins.isolation: host`, where none of this applies.
`hermes plugins validate` reports the isolation mode for exactly this reason.

## 11. Privacy boundary

Three gates, in order:

1. **No content in the handlers.** `on_post_api` copies a fixed field list; it
   never touches `messages`, `response`, `content`, or tool payloads.
2. **No content in the snapshot.** `snapshot()` builds a new dict of numbers and
   identifiers; there is no key for text.
3. **No content on disk.** `history.py` writes `RECORD_FIELDS` only.

Tests assert this with payloads that carry a marker string in prompt/response/
argument/result fields, then scan the snapshot, the broadcast payload and the
written JSON for it.

## 12. Limitations

* The desktop half is materialised into `<HERMES_HOME>/desktop-plugins/` by the
  Electron app **at startup**; there is no CLI trigger, so the chip appears after
  a restart.
* A run with no desktop client (`hermes chat`, cron) aggregates and persists, but
  broadcasts to nobody — counting on the `broadcast_errors` is not an error state.
* TTFT needs `first_chunk_at`; providers that do not report it yield no sample.
* Cache hit rate is a ratio of reported token buckets, not a billing verdict.
* `/api/plugins/*` routes require the dashboard's auth ticket; unauthenticated
  requests get a masked 404.
