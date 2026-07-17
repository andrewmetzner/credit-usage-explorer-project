# Credit Usage Explorer — Program Documentation

*Brookhaven National Laboratory, Information Technology Division*

## Preface

This document describes the Credit Usage Explorer, a Python/Flask web application developed to monitor, forecast, and govern OpenAI credit consumption at Brookhaven National Laboratory (BNL).

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

The application's scope spans five functional areas, each covered in this document:

- **Data collection and processing** -- ingesting, cleaning, validating, and merging usage exports from OpenAI's workspace analytics and administrative API.
- **Deterministic and probabilistic forecasting** -- a weighted-average burn model, a Monte Carlo simulation, and a stabilized linear-regression model, each estimating future credit consumption and projected exhaustion dates.
- **Governance and optimization** -- per-user weekly cap utilization, pressure classification, and tier-adjustment recommendations.
- **Narrative analytics** -- stories/storyboard and alert rules that surface notable usage patterns (approaching a monthly cap, prolonged inactivity, concurrent Pro/Codex usage) without requiring an administrator to read raw tables.
- **Visualization and interaction** -- a Flask/Jinja/Bootstrap/Chart.js front end presenting all of the above as interactive dashboards.

## 2. System Architecture

The application is a Python 3 / Flask project with no external database; all state is held in flat files (CSV, YAML, and JSON) under `data/` and `config/`. The entry point `run.py` calls `app.create_app()`, which constructs the Flask application and its dependency graph.

`create_app()` assembles three long-lived singletons, held in a `Services` container that every blueprint depends on:

| Singleton | Module | Responsibility |
|---|---|---|
| `DataStore` | `app/shared/data_store.py` | Holds the loaded usage `DataFrame` in memory; reloads live after each upload. |
| `IngestionPipeline` | `app/shared/ingestion.py` | Maintains the historical and weekly-operational summary CSVs, the upload log, and the forecast snapshot store. |
| `AppConfig` | `app/shared/config_service.py` | Reads and writes all YAML/JSON configuration: contract terms, tier policy, per-user tier assignments and history, alert rules, and notes. |

`Services` additionally exposes a per-request, memoizing `GovernanceService` and a `build_forecasting_service()` factory — the single point at which a `ForecastingService` is constructed, preferring the ingestion pipeline's summary tables and falling back to deriving weekly summaries directly from the raw daily frame.

Two cross-cutting behaviors are registered at application boot. First, a context processor evaluates `compute_alerts()` on every request to populate the navbar notification bell; unread counts are tracked in server-side read-state that automatically prunes resolved conditions so that recurring issues re-notify. Second, a `before_request` guard redirects any request to the setup wizard (`/setup`) until a contract configuration exists, a check that can be skipped for the remainder of a session. A Jinja filter, `fmt_status`, maps the `SNAKE_CASE` status codes used throughout the analytical layer (e.g. `EXHAUSTION_RISK`, `HIGH_PRESSURE_90_PLUS`) to human-readable labels wherever they are rendered.

## 3. Data Layer

### 3.1 Raw Records

`CreditUsageData` (`app/shared/data_store.py`) loads the current usage file (`.xlsx` or `.csv`), normalizes its columns, and parses the `usage_type` field into derived columns (`usage_type_parsed_type`, `usage_type_model`, and related fields). The columns used throughout the application are `email`, `date_partition` (the calendar day of the record), `usage_credits`, one or more `usage_type_*` columns, `name`, and `department`.

### 3.2 Upload and Merge

Because usage exports are uploaded repeatedly and routinely overlap, the merge logic in `app/dashboard/upload_routes.py` and `app/shared/data_merge.py` is deliberately order-independent. Each file's `date_partition` values are canonicalized to ISO format before files are combined. Rows are deduplicated on a natural record key composed of the user identifier columns, the date, and the usage type. When two files disagree on the recorded `usage_credits` value for the same record, the larger value is kept — a `max` operation being commutative guarantees the same merged result regardless of upload order. Derived `usage_type_*` columns are dropped and recomputed after every merge to avoid stale derivations.

### 3.3 Corrections View

`corrected_usage_view()` (`app/shared/data_filters.py`) is a non-destructive read-time view that rolls API usage records (`usage_type` values matching `^api\.`, or a model name ending in `(API)`) up under Codex for reporting purposes. The underlying source rows are never modified; the correction exists only at the point of display and aggregation.

### 3.4 Ingestion Pipeline

Uploads made from the Settings page feed two summary tables maintained in `data/processed/` by `app/shared/ingestion.py`:

- **Historical summary** (`ingest_historical`) — one row per pre-contract period, recording `period_start`, `period_end`, and `total_credits_used`.
- **Weekly operational summary** (`ingest_weekly`) — one row per contract week, recording `week_start`, `week_end`, `total_credits_used`, and `unique_users`, with duplicate-week protection; the week can be inferred from the uploaded filename.

The ingestion pipeline also owns the forecast snapshot store: `forecast_history.csv` plus a `snapshots/<timestamp>.json` file per snapshot, holding the complete time series (actual burndown, deterministic forecast, Monte Carlo bands, and machine-learning bands) captured at that point in time. When no pipeline summaries exist yet, `ForecastingService` derives weekly summaries directly from the daily data-store frame, anchoring weeks on Monday and treating any week ending before the contract start date as historical.

## 4. Configuration

### 4.1 Contract Configuration (`config/contract_config.yaml`)

The `contract` block records the contract start and end dates, the total purchased credits, and the credit ledger (`credit_entries`, described in §6.5). The `pricing` block records the current and next-contract price per credit. The `forecast` block records the weighting mode (`auto`, described in §6.2), the auto-weight schedule, the recent-average window (four weeks), the number of Monte Carlo runs (10,000 by default), and the snapshot auto-save policy (`daily`, `on_upload`, or `both`).

### 4.2 Tier Policy Configuration (`config/tier_policy_config.yaml`)

`cap_period` determines whether stored `credit_cap` figures represent weekly or monthly totals; the workspace currently uses `monthly`. `weeks_per_month` determines how a monthly cap is converted to a weekly pace — either a fixed value (e.g. 4.0 or 4.345) or the literal `actual`, which divides by the true number of Mondays in the cap's calendar month. `cap_period_change_date` records the date on which the workspace switched from weekly to monthly caps, so that historical weeks are still evaluated against the old weekly regime. The `tiers` block defines roughly eighteen named governance tiers with their monthly credit caps (ranging from Baseline at 400 credits to Emergency at 24,000 credits), each of which can be individually locked against edits in the UI.

### 4.3 Supporting Configuration Files

Per-user governance state is tracked in `user_tier_assignments.json`, `user_tier_history.json`, `user_codex_access.json`, and `tier_change_log.json`. Alerting is configured through `alert_rules.json` and `story_alert_rules.json`, with read/unread state tracked in `alert_read_state.json`. Per-user free-text annotations are stored separately as notes.

## 5. Application Pages and Blueprints

The application is organized into five Flask blueprints, each rendering a distinct set of pages.

### 5.1 Dashboard (`main`, `app/dashboard/`)

The root route redirects to **/summary**, which presents KPI cards (total credits, total records, total users, and the most recent week), a stacked bar chart of weekly credits by usage type, a trend of active users over time, and rankings of top users and departments. **/records** presents the raw usage records in a condensed, filterable table with CSV export. **/alerts** and **/notifications** manage alert-rule and story-rule configuration and the bell's read/unread state. **/setup** is the first-run configuration wizard, collecting contract dates, purchased credits, and the initial data upload. **/debug** and **/debug.json** expose an object-oriented diagnostics framework (`app/shared/diagnostics.py`) in which each `Check` subclass reports an ok/warn/error status that rolls up into an overall system-health indicator. **/upload-data** accepts multi-sheet uploads and applies the order-independent merge logic described in §3.2.

### 5.2 Analytics (`analytics`, `app/analytics/`)

**/leaderboard** ranks users, departments, and models by credit consumption over a configurable time window, filterable by governance tier, with CSV export. **/user-cards** presents per-user summary cards alongside an advanced outlier-search mode (`app/shared/outliers.py`) offering three views — per-user-window, per-user-day, and per-record — matching the metrics used by the alert engine. **/user-summary** is the per-user detail page: a weekly usage-type chart overlaid with the user's weekly cap as a dashed line, a usage-type breakdown, narrative stories (§7), monthly pace history, the user's optimization recommendation card with per-week utilization history, tier-change history, pinned notes, and any alerts referencing the user. Each section of this page is assembled independently and degrades to an empty state on failure rather than breaking the page as a whole. **/storyboard** presents organization-wide story conditions, active alerts, and pinned notes. **/story-matches** lists the users who triggered a specific story-alert rule over a given lookback window.

### 5.3 Forecast (`forecast`, `app/forecast/`)

The **/forecast** page is the primary decision-support surface of the application; its mathematics are described in full in §6. It presents a KPI strip (credits remaining, weekly burn, weeks remaining, projected exhaustion date, pacing status, Monte Carlo risk classification, and machine-learning trend slope) and the Credit Burndown chart, which overlays the actual remaining-credits series (at weekly or daily granularity) with the deterministic projection and optional Monte Carlo (P10/P50/P90) and linear-trend bands fetched lazily from `/forecast/model-data`. The chart also supports overlaying prior snapshots for comparison, drawing credit-event markers where the ledger records a mid-contract grant, a what-if simulation of additional purchased credits, adjustable x-axis windowing, per-series color persistence, PNG export, and a togglable legend. Supporting charts present weekly burn, cumulative burn, active users, and usage-type breakdowns. The **burn window** control restricts which historical weeks feed the burn-rate estimators without altering the credits-remaining or weeks-remaining figures used elsewhere. **Preview mode** allows contract or forecast-weight parameters to be temporarily overridden via query parameters without persisting the change. **Snapshots** capture the full forecast state (auto-saved daily and/or on upload, or saved manually with a label) and can be overlaid on the live chart or backfilled retroactively, week by week, from `snap_generate.html`.

### 5.4 Optimization (`optimization`, `app/optimization/`)

**/optimization** presents a sortable table of tier recommendations (§6.7) with a per-user tier dropdown, apply-one and apply-all actions, a reset-to-tierlist action, and CSV export. Every tier change is recorded to `tier_change_log.json` together with its source (`manual`, `recommendation`, or `tierlist import`).

### 5.5 Settings (`settings`, `app/settings/`)

The Settings blueprint provides the contract and pricing editor, the credit-ledger editor (§6.5), the tier-cap editor with a per-tier lock, tierlist import from `.xlsx`/`.csv` (populating assignments, history, and Codex-access flags via `app/shared/tier_import.py`), historical and weekly data upload, snapshot configuration, data export, and a clear-all operation for resetting application state.

## 6. Mathematical Foundations

The core forecasting and governance mathematics implemented in this application originate from the [BNL AMU forecast model](https://github.com/AleBry/bnl_amu_forecast_model), and the notation below follows the static specification exported from that methodology in `docs/bnl_amu_math_spec.md`. Sections 6.1–6.2 restate the reference model's weighted burn forecast essentially as-is; §6.6–6.7 restate its cap-utilization, pressure-flag, and tier-recommendation rules. From that base, this application extends the reference methodology in three concrete ways, each covered where it belongs below: a **credit ledger** (§6.5) replacing a single static purchased-credits figure with a dated, event-based pool; a **monthly cap-period conversion** (§6.6) layered under the reference model's flat weekly caps; and a **machine-learning trend model** (§6.4) added alongside the reference model's deterministic and Monte Carlo forecasts as a third, independent projection. §6.8 briefly summarizes the parts of the reference methodology — concentration metrics, a policy scenario sandbox, and a weighted multi-signal recommendation engine — that have not been carried into this codebase.

Notation used throughout: `R` = credits remaining, `P` = total purchased credits (the ledger total), `W` = forecast weekly burn, `T` = weeks remaining in the contract, and $y_1, \dots, y_n$ = observed weekly burn totals in chronological order. For the full exported specification the sections below are checked against, see `docs/bnl_amu_math_spec.md`, sourced from [AleBry/bnl_amu_forecast_model](https://github.com/AleBry/bnl_amu_forecast_model).

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

A pace ratio of exactly 1.0 indicates that credit consumption is perfectly proportional to elapsed contract time.

### 6.2 Deterministic (Weighted) Forecasting Model

**Code mapping:** `app/forecast/service.py` / `prediction.py`, `get_forecast()`

The deterministic forecast blends three estimators drawn from processed weekly usage data: the mean historical weekly burn ($H$), the mean of the most recent four in-contract weeks ($A$), and the latest in-contract week's burn ($L$):

$$
W = w_H H + w_A A + w_L L
$$

Weights are selected automatically according to how many operational weeks of data have accumulated, renormalizing across whichever estimators are currently available (matching `select_auto_weights()` / `normalize_active_weights()` in the reference specification):

$$
w_k^{*} = \frac{w_k}{\sum_{j \in \mathcal{A}} w_j}, \qquad \mathcal{A} = \{\text{weights that are present, positive, and matched to an available component}\}
$$

| Operational weeks | $w_H$ | $w_A$ | $w_L$ |
|---|---|---|---|
| 0–2 | 0.7 | — | 0.3 |
| 3–4 | 0.5 | 0.3 | 0.2 |
| 5–8 | 0.3 | 0.5 | 0.2 |
| 9+ | 0.2 | 0.6 | 0.2 |

From $W$, the remaining forecast quantities follow directly:

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

The burndown projection charted on the Forecast page steps forward one week at a time as $\text{rem}_i = \max(R - W \cdot i,\, 0)$, but additionally inserts the exact zero-crossing day so that the chart reads zero precisely on the projected exhaustion date, rather than on the last partial-week remainder.

### 6.3 Monte Carlo Simulation

**Code mapping:** `app/forecast/prediction.py`, `MonteCarloModel` (default 10,000 runs, fixed seed)

Rather than assuming a parametric noise distribution, volatility is sampled empirically from the observed weekly burn totals themselves. Each observed week contributes a burn multiplier:

$$
m_j = \frac{y_j}{\bar y}
$$

If fewer than two clean observations exist, or the mean observed burn is not positive, the model falls back to a multiplier of 1.0. For simulation run $i$ and future week $t$, a multiplier is drawn uniformly with replacement from $\{m_j\}$:

$$
\text{burn}_{i,t} = \max(W \cdot m,\, 0) \times \text{frac}_t
$$

$$
\text{rem}_{i,t} = \max\!\left(R - \sum_{s \le t} \text{burn}_{i,s},\, 0\right)
$$

where $\text{frac}_t$ equals 1 for a full week and less than 1 for a trailing partial week. The full simulation is a single vectorized run × step NumPy operation. Results are summarized by percentile at each future week:

$$
\text{exhaustion probability} = \frac{\text{number of runs with contract-end remaining} \le 0}{\text{total simulation runs}}
$$

$$
\text{stranding probability} = \frac{\text{number of runs ending above the stranding threshold}}{\text{total simulation runs}}
$$

P50 is plotted as the center projection line; the P10–P90 interval forms the shaded confidence band. The reference specification additionally defines a combined risk classification from exhaustion and stranding probability (`risk status`: high/moderate exhaustion risk, high/moderate stranding risk, or balanced); this application computes and surfaces `exhaustion_probability` but does not compute a stranding probability or the combined classification — see §6.8.

### 6.4 Machine Learning (Linear Trend) Model

**Code mapping:** `app/forecast/prediction.py`, `LinearRegressionModel` (`stabilized_v2`)

An ordinary least-squares regression is fit to the chronological in-contract weekly burn totals, using scikit-learn where available and an `np.linalg.lstsq` fallback otherwise:

$$
y_t \approx a + bt, \qquad t = 0, \dots, n-1
$$

$$
r^2 = 1 - \frac{SS_{\text{res}}}{SS_{\text{tot}}}, \qquad \sigma = \text{std}(\text{residuals},\ \text{ddof}=1)
$$

A raw extrapolation of this line is prone to whipsawing on short or noisy histories, so the projected weekly burn is stabilized by blending it back toward the deterministic forecast $W$. The blend weight depends on the number of observed weeks and the regression's goodness of fit, and is halved further when the fitted slope is implausibly steep relative to mean observed burn:

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

An uncertainty band around this projection accumulates residual noise over the forecast horizon (using $z = 1.2816$, the two-sided 80% critical value, so the band corresponds to P10/P90):

$$
\text{var}_k = \sum_{j \le k} (\sigma \cdot \text{frac}_j)^2, \qquad \text{band}_k = 1.2816 \sqrt{\text{var}_k}
$$

$$
P90_k = \min(\text{rem}_k + \text{band}_k,\ P), \qquad P10_k = \max(\text{rem}_k - \text{band}_k,\ 0)
$$

The model additionally reports an effective slope ($w \cdot b$), a trend direction relative to the threshold $\max(0.02\,\bar y,\ 1)$ credits/week, and a qualitative fit label (`low_history`, `strong_fit` for $r^2 \ge 0.70$, `moderate_fit` for $r^2 \ge 0.35$, or `weak_fit`). With fewer than two observed weeks the model falls back to the deterministic forecast's shape.

**Program extension:** the reference model's exported specification (§1–§8) defines only the deterministic weighted forecast and the Monte Carlo simulation; it has no linear-regression component. The stabilized OLS trend model above is this application's own addition, providing a third, independently computed projection — with its own uncertainty band — that administrators can compare against the deterministic and Monte Carlo views on the same chart.

### 6.5 Credit Ledger

**Code mapping:** `app/shared/credit_ledger.py`

The contract's total credit pool is represented as a list of dated entries of kind `purchased`, `gifted`, or `adjustment`, rather than a single static figure:

$$
\text{available}(d) = \sum_{\text{entries } e \text{ with effective date} \le d} \text{credits}(e)
$$

$$
\text{remaining}(d) = \text{available}(d) - \text{cumulative usage}(d)
$$

Every downstream computation reads from `remaining(d)`, so a mid-contract grant (e.g. the 200,000-credit gift added on 2026-06-10) appears on the burndown chart as a discrete step up on its effective date, rather than retroactively inflating the credits-remaining figure for the entire contract history. Ledger entries are clamped to the contract start date, and `purchased_credits` is kept synchronized as the running ledger total. Each mid-contract entry is rendered as a green dotted vertical marker on the burndown chart, individually labeled (e.g. "+200,000 Gifted / grace") and toggleable via the Credit markers control.

**Program extension:** the reference model treats `P` (purchased credits) as a single static figure. This application replaces that with the dated ledger above, so that gifts and adjustments made mid-contract are reflected accurately at the point they took effect rather than smeared across the whole history — every other formula in §6.1–6.4 that references `P` or `R` reads from this ledger rather than a fixed constant.

### 6.6 Governance and Cap-Pressure Mathematics

**Code mapping:** `app/optimization/service.py`

All governance math operates on a weekly basis. Because tier caps are configured as monthly totals (`cap_period: monthly`), each cap is first converted to a weekly pace:

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

Because a week is assigned to the month of its Monday, and `weeks_in_month` counts Mondays, a tier's weekly caps across any given month sum exactly to its configured monthly cap. Weeks whose Monday falls before `cap_period_change_date` are evaluated under the legacy flat-weekly regime (dividing by 4.0) rather than the monthly-derived pace, so historical governance decisions remain consistent with the caps that were actually in force at the time.

Per user-week, utilization and pressure follow directly (matching §9–§10 of the reference specification):

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

A user's effective governance tier is resolved from the tier assignment (`resolve_governance_assignments`), with two special rules: unassigned users default to Baseline, and Codex-access groups (whose names begin with "codex") resolve back to the user's most generous *real* governance tier from their assignment history, unless Codex access is the user's only recorded tier, in which case it is kept as their governance tier.

**Program extension:** the reference model's cap-utilization and pressure-flag formulas (§9–§10) assume a flat weekly cap. This application's tiers are configured as *monthly* totals, so the `weeks_per_month` / `weeks_in_month` conversion above — including the regime switch at `cap_period_change_date` — is layered underneath the reference formulas to convert each tier's monthly cap into the correct weekly pace before `cap_utilization` and `pressure_flag` are evaluated exactly as specified.

### 6.7 Tier Recommendation Rules

**Code mapping:** `app/optimization/service.py`, `_recommended_action()`; corresponds to §13 of the reference specification ("legacy" recommendation rules)

$$
\text{recommended action} =
\begin{cases}
\text{monitor — more history needed}, & \text{weeks observed} < 2 \\
\text{consider move up tier}, & \text{weeks observed} \ge 3 \text{ and share of weeks} \ge 90\% \text{ cap} \ge 0.50 \\
\text{consider move down tier}, & \text{weeks observed} \ge 4 \text{ and avg. \& latest utilization} \le 0.25 \\
\text{monitor recent spike}, & \text{latest utilization} \ge 0.90 \\
\text{no change}, & \text{otherwise}
\end{cases}
$$

Where a tier move is recommended, the target tier is selected by walking a preferred credit-tier ladder (Baseline → Advanced Credit Users → High Credit Consumption Users → One K Credit Users → Emergency Credit Users) one step in the recommended direction:

$$
\text{recommended cap change} = \text{recommended weekly credit cap} - \text{latest weekly credit cap}
$$

$$
\text{estimated avg. utilization after change} = \frac{\text{avg. weekly credits used}}{\text{recommended weekly credit cap}}
$$

Recommendations are ranked for display by action priority (move up, move down, monitor spike, monitor — needs history, no change, in that order), then by latest utilization, then by total credits consumed.

### 6.8 Reference Methodology Not Yet Adopted

The reference model's exported specification (`docs/bnl_amu_math_spec.md`, §11 and §15–20) also defines three mechanisms this application has not carried over. Full formulas are in the spec; each is summarized here in brief:

- **Concentration metrics and a composite cap-pressure index** — a Herfindahl-Hirschman Index (HHI) over per-user usage share, combined with utilization and threshold pressure into a single 0–100 index. This application computes only a top-10%-consumption-share figure (§5.3, latest-week summary); it does not compute HHI or a composite index.
- **A policy scenario sandbox** — dynamic contract-sizing (required size plus a buffer percentage) and a re-weighted exhaustion/stranding probability for a hypothetical what-if scenario, classified into `CRITICAL` / `WARNING` / `OVERSIZED` / `BALANCED`. The Forecast page's what-if extra-credits control (`static/js/forecast.js`) is a lighter, client-side approximation — it re-plots the burndown as if additional credits were added, without contract-sizing or the scenario status classification.
- **A weighted, multi-signal recommendation engine** — move-up/move-down/review scores built from multiple weighted signals (pressure, historical heavy/light use, emergency flags), with a reported confidence level. This application's recommendation logic (§6.7) is the reference model's simpler single-rule-chain alternative to this engine, not the engine itself.

## 7. Stories and Alerts

### 7.1 Stories (`app/analytics/stories.py`)

Stories are short, narrative insights generated per user, each independently computed and rendered only when applicable:

- **Month pace** — the monthly budget for a given calendar month is $\text{weekly cap}(\text{tier}, \text{last week of month}) \times \text{weeks in month}(\text{month})$, which equals the configured monthly cap after the weekly→monthly switch, or the flat weekly cap times the weeks in that month beforehand. The story reports spend as a share of that allowance, the dates on which 25%, 50%, 75%, and 100% of the allowance were crossed (computed from the daily cumulative sum), and, if applicable, the day the cap was reached. It is toned as an alert at 100% or more of allowance and as notable at 80% or more.
- **Activity recency** — the gap, in days, between a user's last active day and the newest date present in the dataset (not the calendar date). Toned as an alert at 30 or more days of inactivity and as notable at 14 or more, and separately reports the user's number of active days within the trailing 14 data-days.
- **Pro + Codex same day** — flags days on which a user issued both Pro-tier prompts and Codex requests, read as a signal of working a difficult problem across multiple tools concurrently.

Story-alert rules (`inactive`, `burst_cap`, and `pro_codex`, each evaluated over a configurable lookback window) are evaluated organization-wide via `evaluate_story_rules()`, using `GovernanceService.monthly_cap_by_email()` for the pace-related computations, and deep-link to `/story-matches` for the list of triggering users.

### 7.2 Alerts (`app/shared/alerts.py`, `app/shared/alert_rules.py`)

`compute_alerts()` runs defensively on every page load — it is designed so that a failure in any one alert source cannot break page rendering — and aggregates four sources:

1. **Stale data** — the newest `date_partition` in the dataset is more than ten days old.
2. **User-defined rules** — threshold rules over a configurable lookback window, evaluated at one of several granularities (`per_record`, `per_user_day`, `per_user_window`, `total_window`, `total_day`, `active_users_window`); the first three deep-link into the outlier-search view.
3. **Story rules**, as described above.
4. **Forecast conditions** — `EXHAUSTION_RISK` (rendered as a danger-level alert) and an `OVERBURNING` pacing status at 1.3× or more of expected pace (rendered as a warning), both computed from the deterministic forecast only, to keep this check inexpensive on every request.

## 8. Front-End Chart Machinery (`static/js/`)

`charts.js` implements the `BNLChart` wrapper used throughout the application, providing PNG export, a fullscreen modal view, theme synchronization against the application's CSS-variable-based light/dark toggle, a crosshair plugin, a shared categorical color palette, and a reusable weekly usage-type stacked-bar component. On the user-summary page, that shared component additionally draws the user's weekly cap as a dashed, stepped line that is regime-aware — correctly stepping at the weekly-to-monthly cap-period switch. A shared `bnlDrawMarkerLabel()` helper renders the label pill used by vertical marker lines, including the forecast chart's credit-event markers.

`forecast.js` implements all burndown-chart logic: construction of the weekly and daily actual-usage series against the credit ledger, the deterministic projection with zero-crossing insertion, lazy fetching and toggling of the Monte Carlo and machine-learning overlay bands, snapshot overlays, the what-if extra-credits simulation, x-axis windowing, per-series color persistence via `localStorage`, and the green dotted `bnl-credit-events` markers (individually hideable via the Credit markers control, with its state persisted as `fc-credit-markers`). `summary.js`, `column_grid.js`, `table_sort.js`, and `alerts.js` implement the summary-page charts, the records-table column chooser, client-side table sorting, and alert read-state calls, respectively.

A number of performance measures are in place: navbar alert computation is cached with a roughly 60-second TTL, invalidated on data upload or configuration-file change; `burst_cap` story-rule matching is vectorized as a users × days matrix rather than iterated per user; the leaderboard computes only the currently active tab rather than all of them; and the records table renders only its first 1,000 rows in the browser, with CSV export carrying the full filtered result set.

## 9. Known Limitations and Parked Work

- **Monthly cap display** (`drafts/monthly_cap_display.md`) — the three UI locations that currently display a user's credit cap (the user-summary optimization card, the per-week history table, and the optimization table's cap-change column) all show the *weekly* pace figure, even though the underlying configuration now stores caps as *monthly* totals. `tier_monthly_caps()` already computes the monthly-equivalent view and is used by the Stories/pace features, but has not yet been threaded into these three display locations. This is a display-only change; it does not affect any utilization, pressure, or recommendation math. It remains parked pending confirmation of the desired presentation (replace the weekly figure outright, or show both).
- **Reference-model methodology not yet adopted** — the concentration metrics (top share, HHI), composite cap-pressure index, policy scenario sandbox, and weighted multi-signal recommendation engine described in §6.8 are part of the upstream reference model's methodology but have no counterpart in this codebase today.

## 10. Conclusion

The Credit Usage Explorer operationalizes its forecasting and governance methodology as a continuously running web application: a Flask backend ingests and merges OpenAI usage exports into structured historical and weekly datasets; a forecasting layer combines a deterministic weighted-average model, an empirical Monte Carlo simulation, and a stabilized linear-regression trend model to project future credit demand and exhaustion risk; a governance layer evaluates individual users against tiered weekly caps and recommends tier adjustments; and a narrative alerting layer surfaces the results of all of the above without requiring an administrator to inspect raw data. Each of these components is documented above with its exact mathematical formulation, tied directly to its implementation in `app/`, so that this document can be re-verified against the source in the same manner as `docs/bnl_amu_math_spec.md` was originally produced.
