# Credit Usage Explorer :: Architecture & Developer Handoff

*A code-oriented guide for whoever maintains this program next. For the
forecasting **math** (contract status, Monte Carlo, ML trend, governance
formulas), see [`program_documentation.md`](program_documentation.md); this
doc is about how the **code** is put together and how to work in it.*

---

## 1. What it is

A local **Flask** web app that ingests OpenAI/ChatGPT credit-usage data,
shows dashboards (summary, per-user, leaderboard, records), **forecasts**
credit burn across one or more contracts, and can **sync usage live** from
the OpenAI Admin API.

**Stack:** Python 3.14 · Flask · pandas · Jinja2 templates · Chart.js (via a
small `BNLChart` wrapper) · Bootstrap 5. 
No database. The dataset is a single CSV (`data/current_data.csv`); 
config is YAML/JSON files under
`config/`.

---

## 2. Running it

```bash
python run.py
python run.py --no-debug
python run.py --server waitress --host 0.0.0.0   # production-ish, shareable
```

`run.py` just builds the app (`app.create_app()`) and serves it. Everything
of substance is inside the `app/` package.

---

## 3. Directory map

```
run.py                     Entry point (arg parsing + serve)
config.py                  Absolute paths for data/config dirs (single source of truth)
app/
  __init__.py              create_app(): wiring, blueprint registration, before-request reload, scheduler start
  shared/                  Cross-cutting services (no HTTP) :: the heart of the app
    data_store.py          DataStore + CreditUsageData: load/parse the CSV, live reload
    services.py            Services container (store, pipeline, config_svc, governance) passed to every blueprint
    config_service.py      AppConfig: read/write contract_config.yaml (multi-contract, migration, CRUD)
    contracts.py           Resolve the "active" contract, sort, rollover
    credit_ledger.py       Per-contract credit entries (purchases/grants), normalization, as-of totals
    utils.py               parse_usage_type() and misc helpers
    data_filters.py        apply_usage_corrections() (view-only tweaks to parsed usage types)
    data_merge.py          merge_usage_data(): dedupe/merge rows on upload or sync
    ingestion.py           Weekly/historical file ingestion pipeline
    chart_data.py          Server-side chart JSON builders
    alerts.py, alert_rules.py   Nav-bell alerts
    governance.py, tier_import.py   Tier assignments
    diagnostics.py         /debug health checks
    csv_export.py          csv_response(), range_slug() shared by export endpoints
  dashboard/               Blueprint "main": summary, records, /data-revision, upload
  analytics/               Blueprint "analytics": leaderboard, user-cards, user-summary, storyboard
  forecast/                Blueprint "forecast": the forecast page + models
    service.py             ForecastingService, contract status, chained projection
    prediction.py          Basic forecasting, Monte Carlo + Linear Regression models
  optimization/            Blueprint "optimization": tier optimization + (optional) tier push
  settings/                Blueprint "settings": contracts, data uploads, Admin API sync/keys
  templates/base.html      Shared layout: sidebar, alerts bell, live-refresh poller
openai_admin_API/          Standalone package: pull usage from the OpenAI Admin API
data/                      current_data.csv (the dataset)
config/                    contract_config.yaml, tier configs, sync config/cursor, lock files
aauth/                     API credentials (git-ignored)
drafts/testfiles/          Unit tests
docs/                      This file + program_documentation.md (the math spec)
```

---

## 4. The core pattern: Services + blueprint factories

`create_app()` builds **one** `Services` object (holds the shared `DataStore`,
`IngestionPipeline`, `AppConfig`, governance, etc.) and passes it into each
blueprint factory:

```python
store   = DataStore(initial_path)
services = Services(store, pipeline, config_svc)
app.register_blueprint(create_dashboard_blueprint(services))
# ... analytics, forecast, optimization, settings
```

Each `create_*_blueprint(services)` closes over `services` and defines its
routes. Inside a blueprint, `store = services.store` and a small
`data()` helper returns `store.data` (the current in-memory dataset). This is
why there's no global state :: everything hangs off the one `Services`.

---

## 5. Data layer (`app/shared/data_store.py`)

- **`current_data.csv`** is the dataset: one row per (date, user, usage_type)
  with `usage_credits`, `usage_quantity`, and a `data_source` column that
  tags where each row came from (`"API GET <UTC timestamp>"` for synced rows,
  blank for manual uploads).
- **`CreditUsageData`** loads that CSV and derives columns: it parses
  `usage_type` into type/model/medium/io (`_add_parsed_usage_type`) and builds
  a display `timestamp` (`_add_timestamp`).
- **`DataStore`** wraps `CreditUsageData` and supports **live reload**:
  - `reload_if_changed()` :: cheap `os.stat` fingerprint (mtime+size); rebuilds
    only when the file actually changed. Called by a **`@app.before_request`**
    hook (`app/__init__.py`) so *any* request re-reads freshly-synced data
    without an app restart, regardless of which process wrote it.
  - `revision` - a token derived from the file fingerprint (`"<mtime>-<size>"`).
    Open pages poll it (see §8). It's file-derived, **not** a counter, so it
    survives an app restart of the same sheet without a false "new data" alarm.

**Rule of thumb:** the CSV on disk is the source of truth; the in-memory
`CreditUsageData` is a cache that any process re-derives from the file.

---

## 6. Configuration (`app/shared/config_service.py`)

`config/contract_config.yaml` holds a list of **contracts** (each with
dates, price/credit, rollover flag, and a `credit_entries` ledger) plus a
global `forecast` block. `AppConfig`:

- Migrates the old single-`contract:` format to the `contracts:` list on
  first load, and re-persists it.
- `load_contract()` returns the **active** contract shaped like the legacy
  single-contract dict (a compatibility shim, so old call sites keep working);
  `resolve_contract_config()` / `app/shared/contracts.py` decide which contract
  is active by date.
- List-based CRUD (`add_contract`, `update_contract_fields`, `remove_contract`,
  `add_credit_entry`, ..) backs the Settings UI.

---

## 7. Pages / blueprints (quick reference)

| Blueprint (name) | URL prefix | Key routes | Notes |
|---|---|---|---|
| dashboard (`main`) | :: | `/summary`, `/records`, `/data-revision`, `/upload` | Records renders capped rows (default 1,000; `?limit=0` = all) with an LRU HTML-fragment cache keyed on `store.revision` |
| analytics | :: | `/leaderboard`, `/user-cards`, `/user-summary`, `/storyboard` | Per-user drill-downs |
| forecast | :: | `/forecast`, `/forecast/model-data` | The burndown chart + MC/ML endpoint |
| optimization | :: | `/optimization`, `/optimization/user-tier` | Tier optimization; optional Admin API tier push |
| settings | `/settings` | contracts CRUD, data uploads, Admin API sync + keys | |

The Records HTML-fragment cache (`_rows_html_cache`) is a small LRU; it keys
on `(store.revision, query_string)` so it survives reloads of the same data
and invalidates when real new data lands.

---

## 8. Live refresh & the "new data" notification

When a sync writes new rows, open pages update **without** a program
restart, and without yanking someone mid-task.

- **Server:** the before-request `reload_if_changed()` (§5) means any request
  already serves fresh data. `/data-revision` returns `{revision, info}` where
  `info` is a short "+N rows, +C credits" summary from the sync config.
- **Client (`base.html` poller):** every 20s it polls `/data-revision`. When
  the revision changes:
  - **Page idle** (no recent interaction, nothing focused, no modal) → reload
    immediately ("just updates").
  - **Page active** → surface a *New data synced* item in the alerts **bell**
    (badge ticks up) + a sidebar "Reload for new data" button; auto-reloads
    once the page goes idle (after certain time, similar to an update left idle for too long)

A manual "Sync now" or file upload does a full navigation, so it shows new
data immediately without the poller.

---

## 9. OpenAI Admin API sync (`openai_admin_API/`)

A **self-contained package** (importable, wrapped in try/except everywhere so
the app still runs if it or its credentials are absent). It pulls usage
("Costs") records and merges them into `current_data.csv`.

- **`client.py` / `endpoints.py`** :: HTTP client + endpoint wrappers.
- **`sync_usage.py`** :: the pull+merge logic:
  - `fetch_new_usage_rows()` pulls Costs since a **cursor** (the end-time of the
    last processed cost-log file; cost-log files are immutable, so already-seen
    files are never re-downloaded). Cursor lives in
    `config/admin_api_sync_cursor.json`; falls back to the last date in the CSV.
  - `sync_once()` = fetch + merge + advance-cursor as **one atomic unit** under
    a cross-process file lock, so two overlapping runs can't double-count.
  - `merge_into_current_data()` writes atomically (temp file + `os.replace`).
  - `_accumulate_api_deltas()` makes same-day re-syncs additive (a later pull
    for a day is a delta to add, not a total to max-merge).
  - Every synced row is tagged `data_source="API GET <UTC pull time>"`.
- **`scheduler.py`** :: a daemon thread started once per process from
  `create_app()`. Reads `config/admin_api_sync_config.json` (enabled flag,
  interval, align-to-hour) each cycle, so Settings changes apply without a
  restart. Guarded by a **cross-process singleton lock**
  (`config/admin_api_scheduler.lock`, keyed by PID) so a stray leftover
  process can't run a second scheduler.
- **Activity log:** `data/testlogs/sync_activity.log` (low-level trail). The
  authoritative "when was this obtained" record is the per-row `data_source`
  tag; the Settings **"Recent data pulls"** panel reconstructs pull history by
  grouping the `API GET` rows in the CSV.

**Multiple keys:** `app/shared/admin_api_credentials.py` stores a list of API
keys, each with a role (`read` / `write` / `read_write`), and "materializes"
whichever key satisfies each role into the fixed files `client.py` reads. This
lets a read-only key be used for pulls while writes (tier push) require a
write key.

---

## 10. Forecasting (high level :: math in `program_documentation.md`)

- **`ForecastingService`** (`app/forecast/service.py`) computes contract status
  (elapsed/remaining, pace) and the deterministic burn projection for the
  active contract, and `build_chained_projection()` continues the line across
  future contracts (expiring or rolling over credits at each boundary).
- **`prediction.py`** :: Monte Carlo and Linear-Regression models, served to the
  chart via `/forecast/model-data` (accepts a `contract_id` + burn window so it
  can forecast an arbitrary contract, not just the active one).
- **`static/js/forecast.js`** :: builds the burndown chart client-side: the
  actual line (capped at *today* -- never a future date), the projection, MC/ML
  bands, and contract-boundary markers. Chart modes: `current`, `chained`,
  `overall`, or a specific contract id.

---

## 11. Performance notes

- **Data reload is vectorized** (~0.1s for 25k rows).
- **Records caps rendered rows** (default 1,000) :: rendering all ~25k rows was
  ~2s of Jinja + a heavy DOM. Filters/sort still apply to the full dataset;
  Export CSV is uncapped.
- **Fragment cache** for the Records tbody, keyed on the data revision.

If a page feels slow, first check whether it's the *first* request after a
sync (the one that pays the reload) vs steady-state, and whether it's
rendering an uncapped row set.

---

## 12. Operational gotchas

- **OneDrive:** the project lives in a OneDrive-synced folder whose filter
  driver can lock `current_data.csv` for 90+ seconds. Writes retry with
  backoff (`_with_file_lock_retry`). The real fix is excluding `data/` from
  OneDrive sync.
- **Reloader orphans:** Flask's debug reloader can leave old `run.py` processes
  alive. They're harmless (the scheduler singleton lock stops double-syncing,
  only one binds the port), but they clutter. `--no-debug` avoids them.
- **Credentials** live in `aauth/` (git-ignored). Nothing there is committed.

---

## 13. Where to extend

- **New page:** add a blueprint under `app/<name>/`, a `create_*_blueprint`
  factory, register it in `create_app()`, add a template + a sidebar link in
  `base.html`.
- **New data source:** produce rows in the `current_data.csv` schema and go
  through `merge_usage_data()` so dedupe applies.
- **New forecast model:** add it in `prediction.py` and wire a toggle in
  `forecast.js` + `/forecast/model-data`.
- **Config:** absolute paths are centralized in `config.py`
```
