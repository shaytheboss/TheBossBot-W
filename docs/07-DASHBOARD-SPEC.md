# 07 — Dashboard Spec

Static HTML/CSS/JS, served by FastAPI at `/`. No build step. Same visual language as `weather-web-checker`.

## Pages

### `/` — Trades + P&L

| Section            | Content                                                                                      |
| ------------------ | -------------------------------------------------------------------------------------------- |
| Header             | Bot logo + status pill (🟢 live / 🟡 paused / 🔴 error)                                       |
| KPI row            | 4 tiles: **Open trades**, **Today P&L**, **Lifetime P&L**, **Win rate**                       |
| P&L chart          | Cumulative P&L line chart (last 30 days). Drawn with vanilla SVG, no chart lib.              |
| Trades table       | Columns: ID, Entered, Station, Side, Entry, Size, Model%, Market%, Edge, Status, Resolved, P&L. Sort by `entered_at` desc. Filter by status (open / won / lost / all). |

### `/markets` — Active markets

Columns: Question (truncated), Station, Resolves on, Comparator + threshold, Our prob, Market YES, Edge, Status. Sortable by edge.

Each row links to:
- Polymarket event URL
- Latest forecast snapshot (modal with ensemble histogram)

### `/settings` — Tunables

Admin-only (token in query string `?key=...` validated server-side against `SETTINGS_ADMIN_TOKEN` env var — simple but enough for one user).

Form fields:
- `min_model_prob` (0.50–0.99, step 0.01)
- `min_edge_pp` (0.01–0.30, step 0.01)
- `paper_size_usd` (10–10000, step 10)
- `enable_trading` (toggle)
- Per-model weights (ECMWF / ICON / GFS) summing to 1.0
- `bias_window_days` (3–60)

Save button POSTs to `PUT /api/settings` with the diff. UI refreshes.

### `/stations` — Stations

Table: ICAO, City, State, Lat/Lon, IANA TZ, Bias factor (°C), Bias samples, Last METAR, Enabled toggle.

Click a row → drawer showing last 14 days of `(forecast_p50, metar_max, residual)`.

### `/diag` — Diagnostics

One-page dump:
- Polymarket Gamma reachability + last error
- CLOB reachability
- Open-Meteo last response time
- METAR last fetch
- Scheduler job-status table (last run, next run, last error per job)

## API endpoints

All endpoints return JSON. Authentication:
- Read endpoints: open (read-only — no PII).
- Write endpoints (`PUT /api/settings`, `POST /api/markets/{cid}/override`): require `Authorization: Bearer {SETTINGS_ADMIN_TOKEN}` header.

```
GET  /api/trades?status=open|won|lost&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=200
GET  /api/trades/{id}
GET  /api/markets?status=open&limit=100
GET  /api/markets/{condition_id}
POST /api/markets/{condition_id}/override   { station_override, comparator, lo_f, hi_f, unit }
GET  /api/stations
PATCH /api/stations/{icao}                  { enabled: bool }
GET  /api/forecasts?station=KBKF&date=YYYY-MM-DD
GET  /api/metar?station=KBKF&hours=24
GET  /api/stats?days=30
GET  /api/settings
PUT  /api/settings                           { min_model_prob, min_edge_pp, ... }
GET  /api/diag
GET  /api/alerts?type=ENTRY&limit=100
```

## Frontend conventions

- File layout:
  - `app/dashboard/index.html` — trades + P&L
  - `app/dashboard/markets.html`
  - `app/dashboard/settings.html`
  - `app/dashboard/stations.html`
  - `app/dashboard/diag.html`
  - `app/dashboard/style.css` — port from `weather-web-checker/style.css`
  - `app/dashboard/app.js` — shared fetch + render helpers
- FastAPI mounts: `app.mount("/", StaticFiles(directory="app/dashboard", html=True))`.
- Each page does its own `fetch('/api/...')` and renders into a placeholder.
- Auto-refresh: trades + open markets every 30 s via `setInterval`.

## Color & status conventions

| State    | Color                                            |
| -------- | ------------------------------------------------ |
| Open     | `--accent` (#4e9af1 blue)                        |
| Won      | `--green` (#2ea043)                              |
| Lost     | `--red`   (#da3633)                              |
| Pending  | `--yellow` (#d29922)                             |
| Stale    | `--text-muted` (#8b949e)                         |

Edge column color:
- Strong YES (≥10pp): solid green bold
- Mild YES (5–10pp): light green
- Strong NO: solid red bold
- Mild NO: light red
- Else: muted

## Mobile

The whole CSS is already responsive in `weather-web-checker/style.css:377–384`. Keep the same breakpoints (≤640px = single column). Tables become horizontally scrollable inside `.table-scroll` containers.

## P&L chart implementation (SVG, no library)

```js
function renderPnlChart(points, container) {
  // points = [{ date: '2026-05-01', cum_pnl: 12.50 }, ...]
  const w = container.clientWidth, h = 160, pad = 20;
  const xs = points.map(p => new Date(p.date).valueOf());
  const ys = points.map(p => p.cum_pnl);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(0, ...ys), y1 = Math.max(...ys);
  const sx = v => pad + (v - x0) / (x1 - x0) * (w - 2 * pad);
  const sy = v => h - pad - (v - y0) / (y1 - y0) * (h - 2 * pad);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${sx(xs[i]).toFixed(1)},${sy(ys[i]).toFixed(1)}`).join(' ');
  container.innerHTML = `
    <svg width="${w}" height="${h}">
      <line x1="${pad}" y1="${sy(0)}" x2="${w - pad}" y2="${sy(0)}" stroke="var(--border)" />
      <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" />
    </svg>`;
}
```

Keep it lightweight. No D3, no Chart.js.
