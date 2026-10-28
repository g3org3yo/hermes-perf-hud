/**
 * Hermes desktop plugin: "Perf HUD".
 *
 * A chip at the bottom-right of the window shows the live numbers, and a
 * popover opens the full table, the local history, and the two controls
 * (reset current statistics / clear history).
 *
 * Where the numbers come from:
 *   - live:   `plugin.perf-hud.stats.update` on the gateway event bridge
 *             (pushed by the Python half, throttled to ~2.5/s);
 *   - first paint + fallback: `GET /api/plugins/perf-hud/snapshot` through ctx.rest.
 *
 * Labels are part of the contract: `TPS` is the core-compatible metric (sum of
 * output tokens over the sum of api_duration, last 10 calls — the same window
 * the CLI status bar uses). The session-wide number is `Session Avg TPS` and is
 * never shown as TPS. There is no independent step counter in Hermes, so the
 * field is `Agent steps*` with the footnote in AGENT_STEPS_NOTE.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */
import {
  Button,
  cn,
  host,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Separator,
  STATUSBAR_AREAS
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const ID = 'perf-hud'
const EVENT = `plugin.${ID}.stats.update`
const AGENT_STEPS_NOTE = '*Equivalent to LLM calls in the current Hermes telemetry API.'

let rest = null

// ------------------------------------------------------------------ helpers

const num = (value, digits = 1) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? '—' : Number(value).toFixed(digits)

const int = (value) =>
  value === null || value === undefined ? '—' : Math.round(Number(value)).toLocaleString('en-US')

const tokens = (value) => {
  const n = Number(value || 0)
  if (!n) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

const duration = (ms) => {
  if (ms === null || ms === undefined) return '—'
  const total = Math.max(0, Math.round(Number(ms)))
  if (total < 1000) return `${total} ms`
  const s = total / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s - m * 60)}s`
}

const clock = (ts) => {
  if (!ts) return '—'
  const d = new Date(Number(ts) * 1000)
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
}

async function call(path, opts) {
  if (!rest) throw new Error('The plugin bridge is not available yet.')
  return rest(path, opts)
}

/** {snapshot, status, error, reload} — live event first, poll as the floor. */
function usePerf(pollMs) {
  const [snapshot, setSnapshot] = useState(null)
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await call('/snapshot')
        if (!alive) return
        if (res && res.snapshot) setSnapshot(res.snapshot)
        if (res && res.status) setStatus(res.status)
        setError('')
      } catch (err) {
        if (alive) setError(String((err && err.message) || err))
      }
    }

    load()
    const timer = window.setInterval(load, Math.max(1000, Number(pollMs) || 5000))
    const off = host.onEvent(EVENT, (event) => {
      const payload = event && event.payload
      if (!payload || !alive) return
      setSnapshot(payload)
    })

    return () => {
      alive = false
      window.clearInterval(timer)
      if (typeof off === 'function') off()
    }
  }, [pollMs, tick])

  return { snapshot, status, error, reload }
}

// ------------------------------------------------------------------- pieces

function Row({ label, value, hint, accent }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-3 border-b border-(--ui-stroke-secondary) py-1',
    children: [
      jsxs('span', {
        className: cn('text-(--ui-text-tertiary)', hint && 'cursor-help underline decoration-dotted'),
        title: hint || undefined,
        children: label
      }),
      jsx('span', {
        className: cn('truncate tabular-nums', accent ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'),
        children: value
      })
    ]
  })
}

function Section({ title, children }) {
  return jsxs('div', {
    className: 'flex flex-col',
    children: [
      jsx('div', { className: 'pt-2 text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)', children: title }),
      ...(Array.isArray(children) ? children : [children])
    ]
  })
}

function HistoryList({ rows }) {
  if (!rows || !rows.length) {
    return jsx('div', { className: 'py-2 text-(--ui-text-quaternary)', children: 'No sessions recorded yet.' })
  }
  return jsx('div', {
    className: 'flex flex-col',
    children: rows.map((row) =>
      jsxs(
        'div',
        {
          className: 'flex flex-col gap-0.5 border-b border-(--ui-stroke-secondary) py-1',
          children: [
            jsxs('div', {
              className: 'flex items-baseline justify-between gap-2',
              children: [
                jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: row.model || 'unknown model' }),
                jsx('span', {
                  className: 'shrink-0 tabular-nums text-(--ui-accent)',
                  children: `${num(row.tps_core)} t/s`
                })
              ]
            }),
            jsxs('div', {
              className: 'flex items-baseline justify-between gap-2 text-(--ui-text-quaternary)',
              children: [
                jsx('span', { className: 'truncate', children: clock(row.updated_at) }),
                jsx('span', {
                  className: 'shrink-0 tabular-nums',
                  children: `${int(row.llm_calls)} calls · ${duration(row.duration_ms)} · ${tokens(row.output_tokens)} out`
                })
              ]
            })
          ]
        },
        row.session_id || `${row.updated_at}`
      )
    )
  })
}

function Controls({ onChange }) {
  const [busy, setBusy] = useState('')
  const [note, setNote] = useState('')

  const run = async (label, path, body) => {
    setBusy(label)
    setNote('')
    try {
      await call(path, { method: 'POST', body: body || {} })
      setNote(label === 'reset' ? 'Current statistics reset.' : 'History cleared.')
      onChange()
    } catch (err) {
      setNote(String((err && err.message) || err))
    } finally {
      setBusy('')
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 pt-2',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Button, {
            variant: 'ghost',
            size: 'sm',
            disabled: Boolean(busy),
            onClick: () => run('reset', '/reset', {}),
            children: 'Reset current statistics'
          }),
          jsx(Button, {
            variant: 'ghost',
            size: 'sm',
            disabled: Boolean(busy),
            onClick: () => run('clear', '/clear-history', {}),
            children: 'Clear history'
          })
        ]
      }),
      note
        ? jsx('div', { className: 'text-(--ui-text-quaternary)', children: note })
        : jsx('div', {
            className: 'text-(--ui-text-quaternary)',
            children: 'Reset clears this plugin\u2019s live counters for the session. Hermes session state is untouched.'
          })
    ]
  })
}

function Panel({ onChanged }) {
  const { snapshot, status, error, reload } = usePerf(5000)
  const [history, setHistory] = useState(null)
  const cfg = (status && status.config) || {}

  const refreshHistory = useCallback(async () => {
    try {
      const res = await call('/history?limit=20')
      setHistory((res && res.sessions) || [])
    } catch {
      setHistory([])
    }
  }, [])

  useEffect(() => {
    refreshHistory()
  }, [refreshHistory])

  const after = () => {
    reload()
    refreshHistory()
    if (onChanged) onChanged()
  }

  const s = snapshot || {}
  const windowNote =
    s.tps_core_window && s.tps_core_window < s.tps_core_window_max
      ? `Core-compatible: sum(output tokens) / sum(api_duration) over the last ${s.tps_core_window} of ${s.tps_core_window_max} calls`
      : 'Core-compatible: sum(output tokens) / sum(api_duration), last 10 calls (same window as the CLI status bar)'

  return jsxs('div', {
    className: 'flex w-80 flex-col gap-1 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('span', { className: 'font-medium text-(--ui-text-secondary)', children: 'Performance' }),
          jsxs('span', {
            className: 'truncate text-(--ui-text-quaternary)',
            children: [s.model || 'no calls yet', s.provider ? ` · ${s.provider}` : '']
          })
        ]
      }),
      error ? jsx('div', { className: 'text-(--ui-danger,#f87171)', children: error }) : null,

      jsxs(Section, {
        title: 'Throughput',
        children: [
          jsx(Row, { label: 'TPS', value: `${num(s.tps_core)} t/s`, hint: windowNote, accent: true }),
          jsx(Row, {
            label: 'Session Avg TPS',
            value: `${num(s.session_avg_tps)} t/s`,
            hint: 'Same ratio over every call in the session — a different metric from TPS, not a replacement for it.'
          }),
          jsx(Row, { label: 'TTFT (median)', value: `${duration(s.ttft_ms)}` , hint: 'first_chunk_at − started_at, median over the session' }),
          jsx(Row, { label: 'API call', value: `${duration(s.api_ms_avg)} avg` })
        ]
      }),

      jsxs(Section, {
        title: 'Calls',
        children: [
          jsx(Row, { label: 'LLM calls', value: int(s.llm_calls), hint: 'One per completed provider request, success or error.' }),
          jsx(Row, {
            label: 'Agent steps*',
            value: int(s.agent_steps),
            hint: AGENT_STEPS_NOTE
          }),
          jsx(Row, { label: 'Turns', value: int(s.turns) }),
          jsx(Row, { label: 'Errors', value: int(s.api_errors) })
        ]
      }),

      jsxs(Section, {
        title: 'Tokens',
        children: [
          jsx(Row, { label: 'Input', value: tokens(s.input_tokens) }),
          jsx(Row, { label: 'Output', value: tokens(s.output_tokens) }),
          jsx(Row, { label: 'Cache read', value: tokens(s.cache_read_tokens) }),
          jsx(Row, { label: 'Cache hit rate', value: s.cache_hit_pct === null || s.cache_hit_pct === undefined ? '—' : `${Math.round(s.cache_hit_pct)}%`, hint: 'cache_read / (cache_read + fresh input)' })
        ]
      }),

      jsxs(Section, {
        title: 'Time',
        children: [
          jsx(Row, { label: 'Duration', value: duration(s.duration_ms) }),
          jsx(Row, { label: 'Tool time', value: `${duration(s.tool_time_ms)} · ${int(s.tool_calls)} calls`, hint: `${Math.round(s.tool_time_share_pct || 0)}% of measured time` }),
          jsx(Row, { label: 'Started', value: clock(s.first_at) })
        ]
      }),

      jsx('div', { className: 'pt-1 text-(--ui-text-quaternary)', children: AGENT_STEPS_NOTE }),
      jsx(Separator, { className: 'my-1' }),

      jsxs(Section, {
        title: 'History (local, performance metadata only)',
        children: cfg.history_enabled === false
          ? jsx('div', { className: 'py-1 text-(--ui-text-quaternary)', children: 'History is disabled in settings.' })
          : jsx(HistoryList, { rows: history })
      }),

      jsx(Separator, { className: 'my-1' }),
      jsx(Controls, { onChange: after })
    ]
  })
}

function Chip() {
  const { snapshot, status } = usePerf(5000)
  const cfg = (status && status.config) || {}
  const [open, setOpen] = useState(false)

  if (cfg.chip_enabled === false) return null

  const s = snapshot || {}
  const tps = num(s.tps_core)
  const ttft = s.ttft_ms === null || s.ttft_ms === undefined ? '—' : `${Math.round(Number(s.ttft_ms))}ms`
  const calls = int(s.llm_calls)
  const metric = cfg.chip_metric || 'tps'
  const label =
    metric === 'calls'
      ? `${calls} calls`
      : metric === 'tps_ttft'
        ? `${tps} t/s · ${ttft}`
        : `${tps} t/s`

  return jsxs(Popover, {
    open,
    onOpenChange: setOpen,
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsxs('button', {
          type: 'button',
          title: 'Live performance — click for details',
          className: cn(
            'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors',
            'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
          ),
          children: [
            jsx('span', {
              className: cn(
                'inline-block h-1.5 w-1.5 rounded-full',
                s.api_errors ? 'bg-(--ui-warning,#fbbf24)' : s.llm_calls ? 'bg-(--ui-success,#4ade80)' : 'bg-(--ui-text-quaternary)'
              )
            }),
            jsx('span', { className: 'tabular-nums', children: label })
          ]
        })
      }),
      jsx(PopoverContent, {
        side: 'top',
        align: 'end',
        className: 'w-auto p-3',
        children: jsx(Panel, { onChanged: () => setOpen(false) })
      })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Perf HUD',
  description: 'Live TPS, TTFT, calls and token metrics for the focused session.',
  register(ctx) {
    rest = ctx.rest
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 205, render: () => jsx(Chip, {}) })
  }
}
