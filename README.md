<<<<<<< HEAD
# Credit Usage Explorer — Program Documentation

*Brookhaven National Laboratory, Information Technology Division*

## Preface

The Credit Usage Explorer is a Python/Flask web application that monitors, forecasts, and governs OpenAI credit consumption at Brookhaven National Laboratory (BNL).

## Table of Contents

1. Purpose and Scope
2. System Architecture
3. Data Layer
4. Configuration
5. Application Pages and Blueprints
6. Mathematical Foundations
   6.1 Contract Status
   6.2 Deterministic (Weighted) Forecasting Model
   6.3 Monte Carlo Simulation
   6.4 Machine Learning (Linear Trend) Model
   6.5 Credit Ledger
   6.6 Governance and Cap-Pressure Mathematics
   6.7 Tier Recommendation Rules
   6.8 Reference Methodology Not Yet Adopted
7. Stories and Alerts
8. Front-End Chart Machinery
9. Known Limitations and Parked Work
10. Conclusion

---

## 1. Purpose and Scope

- **Data collection and processing** — ingest, clean, validate, and merge usage exports from OpenAI's workspace analytics and administrative API.
- **Forecasting** — a weighted-average burn model, a Monte Carlo simulation, and a stabilized linear-regression model, each projecting future consumption and exhaustion dates.
- **Governance and optimization** — per-user weekly cap utilization, pressure classification, and tier-adjustment recommendations.
- **Narrative analytics** — story and alert rules surfacing notable patterns (approaching a monthly cap, prolonged inactivity, concurrent Pro/Codex usage) without requiring an admin to read raw tables.
- **Visualization** — a Flask/Jinja/Bootstrap/Chart.js front end presenting all of the above as interactive dashboards.

## 2. System Architecture

Python 3 / Flask, no external database; all state lives in flat files (CSV, YAML, JSON) under `data/` and `config/`. `run.py` calls `app.create_app()`, which builds the Flask app and its dependency graph.

`create_app()` assembles three long-lived singletons in a `Services` container that every blueprint depends on:

| Singleton | Module | Responsibility |
|---|---|---|
| `DataStore` | `app/shared/data_store.py` | Holds the loaded usage `DataFrame` in memory; reloads after each upload. |
| `IngestionPipeline` | `app/shared/ingestion.py` | Maintains the historical and weekly-operational summary CSVs, the upload log, and the forecast snapshot store. |
| `AppConfig` | `app/shared/config_service.py` | Reads/writes all YAML/JSON configuration: contract terms, tier policy, per-user tier assignments and history, alert rules, notes. |

`Services` also exposes a per-request, memoizing `GovernanceService` and a `build_forecasting_service()` factory, the single point that constructs a `ForecastingService` — preferring the ingestion pipeline's summary tables, falling back to deriving weekly summaries from the raw daily frame.

Two cross-cutting behaviors run at boot: a context processor evaluates `compute_alerts()` on every request to populate the navbar bell (unread counts are server-side, auto-pruning resolved conditions so recurring issues re-notify); and a `before_request` guard redirects to the setup wizard (`/setup`) until a contract configuration exists, skippable for the rest of a session. A Jinja filter, `fmt_status`, maps the `SNAKE_CASE` status codes used throughout the analytical layer (e.g. `EXHAUSTION_RISK`, `HIGH_PRESSURE_90_PLUS`) to human-readable labels.

## 3. Data Layer

### 3.1 Raw Records

`CreditUsageData` (`app/shared/data_store.py`) loads the current usage file (`.xlsx` or `.csv`), normalizes columns, and parses `usage_type` into derived columns (`usage_type_parsed_type`, `usage_type_model`, and related). Columns used throughout: `email`, `date_partition` (calendar day), `usage_credits`, one or more `usage_type_*` columns, `name`, `department`.

### 3.2 Upload and Merge

Uploads overlap routinely, so the merge logic (`app/dashboard/upload_routes.py`, `app/shared/data_merge.py`) is order-independent: `date_partition` values are canonicalized to ISO before combining, rows are deduplicated on a natural key (user identifier, date, usage type), and where two files disagree on `usage_credits` the larger value wins — `max` being commutative guarantees the same merge result regardless of upload order. Derived `usage_type_*` columns are dropped and recomputed after every merge.

### 3.3 Corrections View

`corrected_usage_view()` (`app/shared/data_filters.py`) is a non-destructive read-time view that rolls API usage records (`usage_type` matching `^api\.`, or a model name ending in `(API)`) up under Codex for reporting. Source rows are never modified — the correction exists only at display/aggregation time.

### 3.4 Ingestion Pipeline

Settings-page uploads feed two summary tables in `data/processed/` (`app/shared/ingestion.py`):

- **Historical summary** (`ingest_historical`) — one row per pre-contract period: `period_start`, `period_end`, `total_credits_used`.
- **Weekly operational summary** (`ingest_weekly`) — one row per contract week: `week_start`, `week_end`, `total_credits_used`, `unique_users`, with duplicate-week protection; week is inferable from the uploaded filename.

The pipeline also owns the forecast snapshot store: `forecast_history.csv` plus a `snapshots/<timestamp>.json` per snapshot, holding the full time series (actual burndown, deterministic forecast, Monte Carlo bands, ML bands) at that point in time. If no pipeline summaries exist yet, `ForecastingService` derives weekly summaries directly from the daily frame, anchoring weeks on Monday and treating weeks ending before the contract start as historical.

## 4. Configuration

### 4.1 Contract Configuration (`config/contract_config.yaml`)

`contract` records the contract start/end dates, total purchased credits, and the credit ledger (`credit_entries`, §6.5). `pricing` records current and next-contract price per credit. `forecast` records the weighting mode (`auto`, §6.2), the auto-weight schedule, the recent-average window (four weeks), Monte Carlo run count (10,000 default), and the snapshot auto-save policy (`daily`, `on_upload`, or `both`).

### 4.2 Tier Policy Configuration (`config/tier_policy_config.yaml`)

`cap_period` sets whether stored `credit_cap` figures are weekly or monthly totals — currently `monthly`. `weeks_per_month` sets how a monthly cap converts to a weekly pace: a fixed value (4.0 or 4.345), or `actual`, which divides by the true number of Mondays in the cap's month. `cap_period_change_date` marks when the workspace switched from weekly to monthly caps, so historical weeks still evaluate against the old regime. `tiers` defines ~18 named governance tiers with monthly credit caps (Baseline at 400 up to Emergency at 24,000), each individually lockable in the UI.

### 4.3 Supporting Configuration Files

Per-user governance state: `user_tier_assignments.json`, `user_tier_history.json`, `user_codex_access.json`, `tier_change_log.json`. Alerting: `alert_rules.json`, `story_alert_rules.json`, with read state in `alert_read_state.json`. Notes are stored separately.

## 5. Application Pages and Blueprints

Five Flask blueprints, each rendering a distinct set of pages.

### 5.1 Dashboard (`main`, `app/dashboard/`)

Root redirects to **/summary**: KPI cards (total credits, records, users, most recent week), a stacked bar chart of weekly credits by usage type, an active-users trend, and top-user/department rankings. **/records** — raw usage records, condensed and filterable, CSV export. **/alerts** and **/notifications** — alert-rule and story-rule configuration and bell read/unread state. **/setup** — first-run wizard (contract dates, purchased credits, initial upload). **/debug** and **/debug.json** — an object-oriented diagnostics framework (`app/shared/diagnostics.py`) where each `Check` subclass reports ok/warn/error, rolled up into overall system health. **/upload-data** — multi-sheet uploads via the order-independent merge (§3.2).

### 5.2 Analytics (`analytics`, `app/analytics/`)

**/leaderboard** — ranks users, departments, and models by consumption over a configurable window, filterable by tier, CSV export. **/user-cards** — per-user summary cards plus an outlier-search mode (`app/shared/outliers.py`) with three views (per-user-window, per-user-day, per-record) matching the alert engine's metrics. **/user-summary** — per-user detail: weekly usage-type chart overlaid with the weekly cap, usage-type breakdown, narrative stories (§7), monthly pace history, the optimization recommendation card with per-week utilization history, tier-change history, pinned notes, and any alerts referencing the user. Each section renders independently and degrades to an empty state on failure. **/storyboard** — org-wide story conditions, active alerts, pinned notes. **/story-matches** — users triggering a given story-alert rule over a lookback window.

### 5.3 Forecast (`forecast`, `app/forecast/`)

**/forecast** is the primary decision-support surface; math in §6. A KPI strip (credits remaining, weekly burn, weeks remaining, projected exhaustion date, pacing status, Monte Carlo risk classification, ML trend slope) and the Credit Burndown chart — actual remaining-credits (weekly or daily) overlaid with the deterministic projection and optional Monte Carlo (P10/P50/P90) and linear-trend bands, fetched lazily from `/forecast/model-data`. The chart supports snapshot overlays, credit-event markers for mid-contract grants, a what-if simulation of additional purchased credits, adjustable x-axis windowing, per-series color persistence, PNG export, and a togglable legend. Supporting charts: weekly burn, cumulative burn, active users, usage-type breakdowns. **Burn window** restricts which historical weeks feed the burn-rate estimators without changing the credits-remaining or weeks-remaining figures elsewhere. **Preview mode** temporarily overrides contract or forecast-weight parameters via query params without persisting. **Snapshots** capture full forecast state (auto-saved daily/on-upload, or saved manually with a label), overlayable on the live chart or backfilled retroactively from `snap_generate.html`.

### 5.4 Optimization (`optimization`, `app/optimization/`)

**/optimization** — sortable tier-recommendation table (§6.7), per-user tier dropdown, apply-one/apply-all, reset-to-tierlist, CSV export. Every tier change is logged to `tier_change_log.json` with its source (`manual`, `recommendation`, or `tierlist import`).

### 5.5 Settings (`settings`, `app/settings/`)

Contract/pricing editor, credit-ledger editor (§6.5), tier-cap editor with per-tier lock, tierlist import from `.xlsx`/`.csv` (via `app/shared/tier_import.py`), historical/weekly data upload, snapshot configuration, data export, and clear-all.

## 6. Mathematical Foundations

The forecasting and governance math originates from the [BNL AMU forecast model](https://github.com/AleBry/bnl_amu_forecast_model); notation follows the static spec exported in `docs/bnl_amu_math_spec.md`. §6.1–6.2 restate the reference model's weighted burn forecast as-is; §6.6–6.7 restate its cap-utilization, pressure-flag, and tier-recommendation rules. This application extends the reference in three ways: a **credit ledger** (§6.5) replacing a single static purchased-credits figure with a dated, event-based pool; a **monthly cap-period conversion** (§6.6) under the reference's flat weekly caps; and a **machine-learning trend model** (§6.4) alongside the deterministic and Monte Carlo forecasts as a third, independent projection. §6.8 summarizes reference-model parts not carried over: concentration metrics, a policy scenario sandbox, and a weighted multi-signal recommendation engine.

Notation: `R` = credits remaining, `P` = total purchased credits (ledger total), `W` = forecast weekly burn, `T` = weeks remaining in the contract, $y_1, \dots, y_n$ = observed weekly burn totals in chronological order. Full spec: `docs/bnl_amu_math_spec.md`, sourced from [AleBry/bnl_amu_forecast_model](https://github.com/AleBry/bnl_amu_forecast_model).

### 6.1 Contract Status

**Code mapping:** `app/forecast/service.py`, `get_contract_status()`

$$
\text{total used} = \text{historical used} + \text{operational used} \quad \text{(in-contract rows only)}
$$

$$
R = P - \text{total used}
$$

$$
T = \frac{\text{contract end} - \text{latest usage date}}{7}
$$

$$
\text{burn pace ratio} = \frac{\text{pct. of credits used}}{\text{pct. of contract elapsed}}
$$

$$
\text{pacing status} =
\begin{cases}
\text{underusing}, & \text{burn pace ratio} < 0.80 \\
\text{on pace}, & 0.80 \le \text{burn pace ratio} \le 1.10 \\
\text{elevated burn}, & 1.10 < \text{burn pace ratio} \le 1.30 \\
\text{overburning}, & \text{burn pace ratio} > 1.30
\end{cases}
$$

A ratio of exactly 1.0 means consumption is perfectly proportional to elapsed contract time.

### 6.2 Deterministic (Weighted) Forecasting Model

**Code mapping:** `app/forecast/service.py` / `prediction.py`, `get_forecast()`

Blends three estimators from processed weekly usage data: mean historical weekly burn ($H$), mean of the most recent four in-contract weeks ($A$), and the latest in-contract week's burn ($L$):

$$
W = w_H H + w_A A + w_L L
$$

Weights are selected automatically by how many operational weeks have accumulated, renormalizing across whichever estimators are available (matching `select_auto_weights()` / `normalize_active_weights()`):

$$
w_k^{*} = \frac{w_k}{\sum_{j \in \mathcal{A}} w_j}, \qquad \mathcal{A} = \{\text{weights that are present, positive, and matched to an available component}\}
$$

| Operational weeks | $w_H$ | $w_A$ | $w_L$ |
|---|---|---|---|
| 0–2 | 0.7 | — | 0.3 |
| 3–4 | 0.5 | 0.3 | 0.2 |
| 5–8 | 0.3 | 0.5 | 0.2 |
| 9+ | 0.2 | 0.6 | 0.2 |

From $W$:

$$
\text{forecast monthly burn} = W \times 4.345
$$

$$
\text{weeks until exhaustion} = \frac{R}{W} \qquad (W > 0)
$$

$$
\text{future usage to contract end} = W \times T
$$

$$
\text{forecast contract-end balance} = R - \text{future usage to contract end}
$$

$$
\text{forecast status} =
\begin{cases}
\text{exhaustion risk}, & \text{forecast contract-end balance} < 0 \\
\text{on target}, & 0 \le \text{forecast contract-end balance} \le 50{,}000 \\
\text{moderate underuse}, & 50{,}000 < \text{forecast contract-end balance} \le 150{,}000 \\
\text{high underuse}, & \text{forecast contract-end balance} > 150{,}000
\end{cases}
$$

The burndown chart steps forward one week at a time as $\text{rem}_i = \max(R - W \cdot i,\, 0)$, inserting the exact zero-crossing day so the chart reads zero on the projected exhaustion date rather than at the last partial-week remainder.

### 6.3 Monte Carlo Simulation

**Code mapping:** `app/forecast/prediction.py`, `MonteCarloModel` (default 10,000 runs, fixed seed)

Volatility is sampled empirically from observed weekly burn totals rather than a parametric distribution. Each observed week gives a burn multiplier:

$$
m_j = \frac{y_j}{\bar y}
$$

With fewer than two clean observations, or a non-positive mean, the model falls back to a multiplier of 1.0. For run $i$, week $t$, a multiplier is drawn uniformly with replacement from $\{m_j\}$:

$$
\text{burn}_{i,t} = \max(W \cdot m,\, 0) \times \text{frac}_t
$$

$$
\text{rem}_{i,t} = \max\!\left(R - \sum_{s \le t} \text{burn}_{i,s},\, 0\right)
$$

$\text{frac}_t$ is 1 for a full week, less than 1 for a trailing partial week. The full simulation is a single vectorized run × step NumPy operation, summarized by percentile at each future week:

$$
\text{exhaustion probability} = \frac{\text{number of runs with contract-end remaining} \le 0}{\text{total simulation runs}}
$$

$$
\text{stranding probability} = \frac{\text{number of runs ending above the stranding threshold}}{\text{total simulation runs}}
$$

P50 is the center projection line; P10–P90 forms the shaded confidence band. The reference spec also defines a combined risk classification from exhaustion and stranding probability (`risk status`); this application surfaces `exhaustion_probability` only — no stranding probability or combined classification (§6.8).

### 6.4 Machine Learning (Linear Trend) Model

**Code mapping:** `app/forecast/prediction.py`, `LinearRegressionModel` (`stabilized_v2`)

Ordinary least-squares fit to chronological in-contract weekly burn totals, using scikit-learn where available and an `np.linalg.lstsq` fallback otherwise:

$$
y_t \approx a + bt, \qquad t = 0, \dots, n-1
$$

$$
r^2 = 1 - \frac{SS_{\text{res}}}{SS_{\text{tot}}}, \qquad \sigma = \text{std}(\text{residuals},\ \text{ddof}=1)
$$

A raw extrapolation whipsaws on short/noisy histories, so the projected weekly burn blends back toward the deterministic forecast $W$. Blend weight depends on observed weeks and goodness of fit, halved further if the slope is implausibly steep relative to mean observed burn:

$$
w =
\begin{cases}
0.20, & n < 4 \\
0.70, & r^2 \ge 0.70 \\
0.50, & r^2 \ge 0.35 \\
0.25, & \text{otherwise}
\end{cases}
\qquad
w \mathrel{{*}{=}} 0.55 \ \text{ if } |b| > 0.35 \times \bar y
$$

$$
C = \max(W,\ \bar y,\ \text{median}(y)), \qquad \text{floor} = 0.18\,C, \qquad \text{cap} = \max(2.25\,C,\ 1.20 \max(y))
$$

For future week $k$:

$$
\text{raw}_k = a + b(n - 1 + k), \qquad \text{blended}_k = w \cdot \text{raw}_k + (1-w) \cdot W
$$

$$
\text{burn}_k = \text{clamp}(\text{blended}_k,\ \text{floor},\ \text{cap}) \times \text{frac}_k, \qquad \text{rem}_k = \max(\text{rem}_{k-1} - \text{burn}_k,\, 0)
$$

The uncertainty band accumulates residual noise over the horizon ($z = 1.2816$, the two-sided 80% critical value — band corresponds to P10/P90):

$$
\text{var}_k = \sum_{j \le k} (\sigma \cdot \text{frac}_j)^2, \qquad \text{band}_k = 1.2816 \sqrt{\text{var}_k}
$$

$$
P90_k = \min(\text{rem}_k + \text{band}_k,\ P), \qquad P10_k = \max(\text{rem}_k - \text{band}_k,\ 0)
$$

The model also reports an effective slope ($w \cdot b$), a trend direction relative to $\max(0.02\,\bar y,\ 1)$ credits/week, and a fit label (`low_history`, `strong_fit` for $r^2 \ge 0.70$, `moderate_fit` for $r^2 \ge 0.35$, else `weak_fit`). Fewer than two observed weeks falls back to the deterministic forecast's shape.

**Program extension:** the reference spec (§1–§8) defines only the deterministic and Monte Carlo forecasts — no linear-regression component. This stabilized OLS trend model is this application's own addition: a third, independently computed projection with its own uncertainty band, comparable on the same chart.

### 6.5 Credit Ledger

**Code mapping:** `app/shared/credit_ledger.py`

The contract's total credit pool is a list of dated entries (`purchased`, `gifted`, `adjustment`), not a single static figure:

$$
\text{available}(d) = \sum_{\text{entries } e \text{ with effective date} \le d} \text{credits}(e)
$$

$$
\text{remaining}(d) = \text{available}(d) - \text{cumulative usage}(d)
$$

Every downstream computation reads from `remaining(d)`, so a mid-contract grant (e.g. the 200,000-credit gift on 2026-06-10) appears on the burndown chart as a discrete step up on its effective date, rather than retroactively inflating the credits-remaining figure across contract history. Ledger entries are clamped to the contract start date; `purchased_credits` stays synchronized as the running ledger total. Mid-contract entries render as green dotted markers on the burndown chart, individually labeled (e.g. "+200,000 Gifted / grace") and toggleable via the Credit markers control.

**Program extension:** the reference model treats `P` as a single static figure. This application replaces it with the dated ledger above, so mid-contract gifts and adjustments reflect accurately at the point they took effect — every other §6.1–6.4 formula referencing `P` or `R` reads from this ledger rather than a fixed constant.

### 6.6 Governance and Cap-Pressure Mathematics

**Code mapping:** `app/optimization/service.py`

Governance math runs weekly. Tier caps are configured as monthly totals (`cap_period: monthly`), so each cap first converts to a weekly pace:

$$
\text{weeks in month}(y, m) = \text{number of Mondays whose date falls within month } m \text{ of year } y
$$

$$
\text{weeks per month}(\text{week start}) =
\begin{cases}
4.0, & \text{week start} < \text{cap period change date} \\
\text{weeks in month}(\text{year}, \text{month}), & \text{setting is "actual"} \\
\text{configured value (e.g. } 4.345\text{)}, & \text{otherwise}
\end{cases}
$$

$$
\text{weekly cap}(\text{tier}, \text{week}) = \frac{\text{monthly credit cap}}{\text{weeks per month}(\text{week start})}
$$

A week is assigned to the month of its Monday, and `weeks_in_month` counts Mondays, so a tier's weekly caps across any month sum exactly to its monthly cap. Weeks whose Monday falls before `cap_period_change_date` use the legacy flat-weekly regime (divide by 4.0) instead, so historical decisions stay consistent with the caps actually in force at the time.

Per user-week, utilization and pressure follow directly (matching §9–§10 of the reference spec):

$$
\text{cap utilization} = \frac{\text{credits used}}{\text{weekly credit cap}}
$$

$$
\text{remaining weekly credits} = \text{weekly credit cap} - \text{credits used}
$$

$$
\text{pressure flag} =
\begin{cases}
\text{above cap } 110+, & \text{cap utilization} \ge 1.10 \\
\text{at or above cap}, & 1.00 \le \text{cap utilization} < 1.10 \\
\text{high pressure } 90+, & 0.90 \le \text{cap utilization} < 1.00 \\
\text{elevated pressure } 80+, & 0.80 \le \text{cap utilization} < 0.90 \\
\text{normal}, & \text{cap utilization} < 0.80
\end{cases}
$$

$$
\text{pressure trend} = \text{latest cap utilization} - \text{first cap utilization}
\quad\Rightarrow\quad
\begin{cases}
\text{increasing}, & \Delta \ge +0.20 \\
\text{decreasing}, & \Delta \le -0.20 \\
\text{stable}, & \text{otherwise}
\end{cases}
$$

A user's effective governance tier resolves from the tier assignment (`resolve_governance_assignments`): unassigned users default to Baseline, and Codex-access groups (names starting "codex") resolve back to the user's most generous *real* governance tier from assignment history, unless Codex access is the only recorded tier — then it's kept as-is.

**Program extension:** the reference model's cap-utilization and pressure-flag formulas (§9–§10) assume a flat weekly cap. This application's tiers are *monthly*, so the `weeks_per_month` / `weeks_in_month` conversion above — including the regime switch at `cap_period_change_date` — is layered underneath to convert each tier's monthly cap into a weekly pace before `cap_utilization` and `pressure_flag` evaluate exactly as specified.

### 6.7 Tier Recommendation Rules

**Code mapping:** `app/optimization/service.py`, `_recommended_action()`; corresponds to §13 of the reference spec ("legacy" recommendation rules)

$$
\text{recommended action} =
\begin{cases}
\text{monitor — more history needed}, & \text{weeks observed} < 2 \\
\text{consider move up tier}, & \text{weeks observed} \ge 3 \text{ and share of weeks} \ge 90\% \text{ cap} \ge 0.50 \\
\text{consider move down tier}, & \text{weeks observed} \ge 4 \text{ and average and latest utilization} \le 0.25 \\
\text{monitor recent spike}, & \text{latest utilization} \ge 0.90 \\
\text{no change}, & \text{otherwise}
\end{cases}
$$

Where a move is recommended, the target tier is selected by walking a preferred credit-tier ladder (Baseline → Advanced Credit Users → High Credit Consumption Users → One K Credit Users → Emergency Credit Users) one step in that direction:

$$
\text{recommended cap change} = \text{recommended weekly credit cap} - \text{latest weekly credit cap}
$$

$$
\text{estimated avg. utilization after change} = \frac{\text{avg. weekly credits used}}{\text{recommended weekly credit cap}}
$$

Recommendations are ranked for display by action priority (move up, move down, monitor spike, monitor — needs history, no change), then latest utilization, then total credits consumed.

### 6.8 Reference Methodology Not Yet Adopted

The reference spec (`docs/bnl_amu_math_spec.md`, §11 and §15–20) defines three mechanisms not carried over here:

- **Concentration metrics and a composite cap-pressure index** — a Herfindahl-Hirschman Index (HHI) over per-user usage share, combined with utilization and threshold pressure into a single 0–100 index. This application computes only a top-10%-consumption-share figure (§5.3, latest-week summary), no HHI or composite index.
- **A policy scenario sandbox** — dynamic contract-sizing (required size plus buffer percentage) and a re-weighted exhaustion/stranding probability for a what-if scenario, classified `CRITICAL` / `WARNING` / `OVERSIZED` / `BALANCED`. The Forecast page's what-if extra-credits control (`static/js/forecast.js`) is a lighter client-side approximation — it re-plots the burndown as if credits were added, without contract-sizing or status classification.
- **A weighted, multi-signal recommendation engine** — move-up/move-down/review scores from multiple weighted signals (pressure, historical heavy/light use, emergency flags) with a reported confidence level. This application's §6.7 logic is the reference model's simpler single-rule-chain alternative, not the engine itself.

## 7. Stories and Alerts

### 7.1 Stories (`app/analytics/stories.py`)

Short, narrative per-user insights, each computed and rendered independently, only when applicable:

- **Month pace** — the monthly budget for a calendar month is $\text{weekly cap}(\text{tier}, \text{last week of month}) \times \text{weeks in month}(\text{month})$: the configured monthly cap after the weekly→monthly switch, or the flat weekly cap times weeks in that month beforehand. Reports spend as a share of that allowance, the dates 25%/50%/75%/100% were crossed (from the daily cumulative sum), and, if applicable, the day the cap was reached. Alert-toned at ≥100% of allowance, notable at ≥80%.
- **Activity recency** — gap in days between a user's last active day and the newest date in the dataset. Alert-toned at ≥30 days inactive, notable at ≥14, and separately reports active days within the trailing 14 data-days.
- **Pro + Codex same day** — flags days with both Pro-tier prompts and Codex requests, read as a signal of working a hard problem across tools concurrently.

Story-alert rules (`inactive`, `burst_cap`, `pro_codex`, each over a configurable lookback window) run org-wide via `evaluate_story_rules()`, using `GovernanceService.monthly_cap_by_email()` for pace computations, deep-linking to `/story-matches` for triggering users.

### 7.2 Alerts (`app/shared/alerts.py`, `app/shared/alert_rules.py`)

`compute_alerts()` runs on every page load, defensively — a failure in one source can't break rendering — and aggregates four sources:

1. **Stale data** — newest `date_partition` more than ten days old.
2. **User-defined rules** — threshold rules over a configurable lookback window at one of several granularities (`per_record`, `per_user_day`, `per_user_window`, `total_window`, `total_day`, `active_users_window`); the first three deep-link into outlier search.
3. **Story rules**, as above.
4. **Forecast conditions** — `EXHAUSTION_RISK` (danger) and `OVERBURNING` pacing at ≥1.3× expected pace (warning), both from the deterministic forecast only, to keep this check cheap on every request.

## 8. Front-End Chart Machinery (`static/js/`)

`charts.js` implements the `BNLChart` wrapper: PNG export, a fullscreen modal, theme sync against the CSS-variable light/dark toggle, a crosshair plugin, a shared categorical palette, and a reusable weekly usage-type stacked-bar component. On user-summary, that component also draws the weekly cap as a dashed, regime-aware stepped line (correct at the weekly→monthly cap-period switch). `bnlDrawMarkerLabel()` renders the label pill for vertical marker lines, including the forecast chart's credit-event markers.

`forecast.js` implements all burndown-chart logic: weekly/daily actual-usage series against the credit ledger, the deterministic projection with zero-crossing insertion, lazy fetch/toggle of Monte Carlo and ML overlay bands, snapshot overlays, the what-if extra-credits simulation, x-axis windowing, per-series color persistence via `localStorage`, and the green dotted `bnl-credit-events` markers (individually hideable, state persisted as `fc-credit-markers`). `summary.js`, `column_grid.js`, `table_sort.js`, `alerts.js` handle summary-page charts, the records-table column chooser, client-side table sorting, and alert read-state calls.

Performance: navbar alert computation caches with a ~60s TTL, invalidated on upload or config change; `burst_cap` matching is vectorized as a users × days matrix rather than per-user iteration; the leaderboard computes only the active tab; the records table renders only its first 1,000 rows, with CSV export carrying the full filtered result set.

## 9. Known Limitations and Parked Work

- **Monthly cap display** (`drafts/monthly_cap_display.md`) — the three UI locations showing a user's credit cap (user-summary optimization card, per-week history table, optimization table's cap-change column) all show the *weekly* pace figure, though config now stores caps as *monthly* totals. `tier_monthly_caps()` already computes the monthly-equivalent view and is used by Stories/pace, but isn't threaded into these three locations yet. Display-only — doesn't affect utilization, pressure, or recommendation math. Parked pending a decision (replace the weekly figure, or show both).
- **Reference-model methodology not yet adopted** — concentration metrics (top share, HHI), composite cap-pressure index, policy scenario sandbox, and weighted multi-signal recommendation engine (§6.8) exist upstream but have no counterpart here.

## 10. Conclusion

The Credit Usage Explorer runs its forecasting and governance methodology as a continuously running web app: a Flask backend ingests and merges OpenAI usage exports into structured historical and weekly datasets; a forecasting layer combines a deterministic weighted-average model, an empirical Monte Carlo simulation, and a stabilized linear-regression trend model to project future demand and exhaustion risk; a governance layer evaluates users against tiered weekly caps and recommends tier adjustments; a narrative alerting layer surfaces all of the above without requiring an admin to inspect raw data. Each component above is documented with its exact math, tied directly to its implementation in `app/`, so this document can be re-verified against source the same way `docs/bnl_amu_math_spec.md` was originally produced.
=======
# credit-usage-explorer-project



## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

* [Create](https://docs.gitlab.com/user/project/repository/web_editor/#create-a-file) or [upload](https://docs.gitlab.com/user/project/repository/web_editor/#upload-a-file) files
* [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://git.bnl.gov/root/credit-usage-explorer-project.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

* [Set up project integrations](https://git.bnl.gov/root/credit-usage-explorer-project/-/settings/integrations)

## Collaborate with your team

* [Invite team members and collaborators](https://docs.gitlab.com/user/project/members/)
* [Create a new merge request](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/)
* [Automatically close issues from merge requests](https://docs.gitlab.com/user/project/issues/managing_issues/#closing-issues-automatically)
* [Enable merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/)
* [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

* [Get started with GitLab CI/CD](https://docs.gitlab.com/ci/quick_start/)
* [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/user/application_security/sast/)
* [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/topics/autodevops/requirements/)
* [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/user/clusters/agent/)
* [Set up protected environments](https://docs.gitlab.com/ci/environments/protected_environments/)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
>>>>>>> af63d662cb621a39a5cde51ea236f570c392c5e8
