from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.shared.typed import DictMixin


@dataclass
class ContractStatus(DictMixin):
    """Where the contract stands right now (credits, pacing, dates)."""

    contract_start_date: Any
    contract_end_date: Any
    latest_usage_date: Any
    purchased_credits: float
    historical_credits_used: float
    operational_credits_used: float
    total_credits_used: float
    credits_remaining: float
    rollover_allowed: bool
    price_per_credit: float
    projected_cost_used: float
    projected_value_remaining: float
    total_contract_days: int
    elapsed_days: int
    remaining_days: int
    weeks_remaining: float
    percent_contract_elapsed: float
    percent_credits_used: float
    burn_pace_ratio: float
    latest_weekly_burn: float
    required_weekly_burn_to_use_all: float
    pacing_status: str


@dataclass
class Forecast(DictMixin):
    """Projected burn and end-of-contract outlook."""

    operational_weeks: int
    historical_avg_burn: float
    latest_week_burn: float
    recent_average_burn: float
    historical_weight_used: float
    latest_week_weight_used: float
    recent_average_weight_used: float
    forecast_weekly_burn: float
    forecast_monthly_burn: float
    credits_remaining: float
    weeks_remaining: float
    weeks_until_exhaustion: float
    forecast_exhaustion_date: Any
    contract_end_date: Any
    forecast_future_usage_to_contract_end: float
    forecast_contract_end_balance: float
    forecast_total_contract_usage: float
    forecast_percent_credits_used_by_contract_end: float
    forecast_status: str


class ForecastingService:
    """
    Computes contract status and credit-burn forecasts.

    Data priority:
      1. Pre-processed pipeline CSVs (historical_df / operational_df) — most accurate.
      2. Raw daily transactional DataFrame (daily_df) — derived into weekly summaries
         automatically when no pipeline data exists.
    """

    def __init__(
        self,
        config: dict,
        historical_df: pd.DataFrame | None = None,
        operational_df: pd.DataFrame | None = None,
        daily_df: pd.DataFrame | None = None,
    ) -> None:
        self.config = config
        self._as_of: pd.Timestamp | None = None
        self._forecast_op_df: pd.DataFrame | None = None

        if historical_df is None and operational_df is None and daily_df is not None:
            historical_df, operational_df = self._derive_from_daily(daily_df)
        elif operational_df is None and daily_df is not None:
            _, operational_df = self._derive_from_daily(daily_df)

        self.historical_df = historical_df
        self.operational_df = operational_df

    def _derive_from_daily(
        self, df: pd.DataFrame
    ) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        if "date_partition" not in df.columns or "usage_credits" not in df.columns:
            return None, None

        wdf = df[["date_partition", "usage_credits"] + (["email"] if "email" in df.columns else [])].copy()
        wdf["_date"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
        wdf = wdf.dropna(subset=["_date"])
        if wdf.empty:
            return None, None

        wdf["_ws"] = wdf["_date"] - pd.to_timedelta(wdf["_date"].dt.dayofweek, unit="D")
        wdf["_we"] = wdf["_ws"] + pd.Timedelta(days=6)

        agg: dict = {"total_credits_used": ("usage_credits", "sum")}
        if "email" in wdf.columns:
            agg["unique_users"] = ("email", "nunique")

        weekly = (
            wdf.groupby(["_ws", "_we"], as_index=False)
            .agg(**agg)
            .sort_values("_ws")
        )

        contract_start = pd.to_datetime(self.config["contract"]["contract_start_date"])

        hist_rows = weekly[weekly["_we"] < contract_start]
        op_rows = weekly[weekly["_ws"] >= contract_start]

        historical = (
            hist_rows.rename(columns={"_ws": "period_start", "_we": "period_end"})
            .reset_index(drop=True)
            if not hist_rows.empty else None
        )
        operational = (
            op_rows.rename(columns={"_ws": "week_start", "_we": "week_end"})
            .reset_index(drop=True)
            if not op_rows.empty else None
        )

        return historical, operational

    def has_data(self) -> bool:
        hist_ok = self.historical_df is not None and not self.historical_df.empty
        op_ok = self.operational_df is not None and not self.operational_df.empty
        return hist_ok or op_ok

    def get_contract_status(self) -> ContractStatus:
        contract = self.config["contract"]
        pricing = self.config["pricing"]

        contract_start = pd.to_datetime(contract["contract_start_date"])
        contract_end = pd.to_datetime(contract["contract_end_date"])
        purchased_credits = float(contract["purchased_credits"])
        rollover_allowed = bool(contract["rollover_allowed"])
        price_per_credit = float(pricing["current_price_per_credit"])

        historical_credits_used = 0.0
        if self.historical_df is not None and not self.historical_df.empty:
            hist_contract = self.historical_df[
                (self.historical_df["period_start"] >= contract_start)
                & (self.historical_df["period_end"] <= contract_end)
            ]
            historical_credits_used = float(hist_contract["total_credits_used"].sum())

        operational_credits_used = 0.0
        latest_weekly_burn = 0.0
        if self.operational_df is not None and not self.operational_df.empty:
            op_contract = self.operational_df[
                (self.operational_df["week_start"] >= contract_start)
                & (self.operational_df["week_end"] <= contract_end)
            ]
            operational_credits_used = float(op_contract["total_credits_used"].sum())
            if not op_contract.empty:
                latest_weekly_burn = float(
                    op_contract.sort_values("week_start").iloc[-1]["total_credits_used"]
                )

        total_credits_used = historical_credits_used + operational_credits_used
        credits_remaining = purchased_credits - total_credits_used

        # latest_usage_date = first day after the last data week ends.
        # week_end / period_end are stored as the last inclusive day of the week,
        # so +1 day gives the exclusive boundary where projections begin.
        dates: list[pd.Timestamp] = []
        if self.historical_df is not None and not self.historical_df.empty:
            dates.append(self.historical_df["period_end"].max() + pd.Timedelta(days=1))
        if self.operational_df is not None and not self.operational_df.empty:
            dates.append(self.operational_df["week_end"].max() + pd.Timedelta(days=1))
        latest_usage_date = max(dates) if dates else contract_start
        if self._as_of is not None and self._as_of > latest_usage_date:
            latest_usage_date = self._as_of

        total_contract_days = (contract_end - contract_start).days
        elapsed_days = max((latest_usage_date - contract_start).days, 0)
        remaining_days = max((contract_end - latest_usage_date).days, 0)
        weeks_remaining = remaining_days / 7 if remaining_days > 0 else 0.0

        pct_elapsed = elapsed_days / total_contract_days if total_contract_days > 0 else 0.0
        pct_used = total_credits_used / purchased_credits if purchased_credits > 0 else 0.0
        burn_pace_ratio = pct_used / pct_elapsed if pct_elapsed > 0 else 0.0

        required_weekly_burn = credits_remaining / weeks_remaining if weeks_remaining > 0 else 0.0

        if burn_pace_ratio < 0.80:
            pacing_status = "UNDERUSING"
        elif burn_pace_ratio <= 1.10:
            pacing_status = "ON_PACE"
        elif burn_pace_ratio <= 1.30:
            pacing_status = "ELEVATED_BURN"
        else:
            pacing_status = "OVERBURNING"

        return ContractStatus(
            contract_start_date=contract_start.date(),
            contract_end_date=contract_end.date(),
            latest_usage_date=latest_usage_date.date(),
            purchased_credits=purchased_credits,
            historical_credits_used=historical_credits_used,
            operational_credits_used=operational_credits_used,
            total_credits_used=total_credits_used,
            credits_remaining=credits_remaining,
            rollover_allowed=rollover_allowed,
            price_per_credit=price_per_credit,
            projected_cost_used=total_credits_used * price_per_credit,
            projected_value_remaining=credits_remaining * price_per_credit,
            total_contract_days=total_contract_days,
            elapsed_days=elapsed_days,
            remaining_days=remaining_days,
            weeks_remaining=weeks_remaining,
            percent_contract_elapsed=pct_elapsed,
            percent_credits_used=pct_used,
            burn_pace_ratio=burn_pace_ratio,
            latest_weekly_burn=latest_weekly_burn,
            required_weekly_burn_to_use_all=required_weekly_burn,
            pacing_status=pacing_status,
        )

    def get_forecast(self) -> Forecast:
        fc_cfg = self.config["forecast"]

        if self.historical_df is not None and not self.historical_df.empty:
            historical_avg_burn = float(self.historical_df["total_credits_used"].mean())
        else:
            historical_avg_burn = float(
                self.operational_df["total_credits_used"].mean()
            ) if self.operational_df is not None and not self.operational_df.empty else 0.0

        contract_start = pd.to_datetime(self.config["contract"]["contract_start_date"])
        contract_end = pd.to_datetime(self.config["contract"]["contract_end_date"])

        # Use _forecast_op_df for burn rate when a date window is set; else fall back to operational_df
        _burn_src = self._forecast_op_df if self._forecast_op_df is not None else self.operational_df
        if _burn_src is not None and not _burn_src.empty:
            op_contract = _burn_src[
                (_burn_src["week_start"] >= contract_start)
                & (_burn_src["week_end"] <= contract_end)
            ].sort_values("week_start")
        else:
            op_contract = pd.DataFrame()

        op_count = len(op_contract)
        latest_week_burn: float | None = None
        recent_avg_burn: float | None = None

        if op_count > 0:
            latest_week_burn = float(op_contract.iloc[-1]["total_credits_used"])

            window = int(fc_cfg.get("recent_average_window_weeks", 4))
            min_weeks = int(fc_cfg.get("minimum_weeks_for_recent_average", window))
            if op_count >= min_weeks:
                recent_avg_burn = float(op_contract.tail(window)["total_credits_used"].mean())

        raw_weights = self._select_weights(op_count)

        usable: dict[str, float] = {}
        if raw_weights.get("historical_weight") is not None:
            usable["historical_weight"] = float(raw_weights["historical_weight"])
        if raw_weights.get("latest_week_weight") is not None and latest_week_burn is not None:
            usable["latest_week_weight"] = float(raw_weights["latest_week_weight"])
        if raw_weights.get("recent_average_weight") is not None and recent_avg_burn is not None:
            usable["recent_average_weight"] = float(raw_weights["recent_average_weight"])

        if fc_cfg.get("normalize_weights", True):
            total = sum(usable.values())
            weights = {k: v / total for k, v in usable.items()} if total > 0 else usable
        else:
            weights = usable

        forecast_weekly = (
            historical_avg_burn * weights.get("historical_weight", 0)
            + (latest_week_burn or 0.0) * weights.get("latest_week_weight", 0)
            + (recent_avg_burn or 0.0) * weights.get("recent_average_weight", 0)
        )

        cs = self.get_contract_status()
        credits_remaining = cs["credits_remaining"]
        weeks_remaining = cs["weeks_remaining"]
        latest_date = pd.to_datetime(cs["latest_usage_date"])
        purchased = cs["purchased_credits"]
        total_used = cs["total_credits_used"]

        forecast_monthly = forecast_weekly * 4.345

        weeks_until_exhaustion: float | None = None
        exhaustion_date = None
        if forecast_weekly > 0:
            weeks_until_exhaustion = credits_remaining / forecast_weekly
            exhaustion_date = (latest_date + timedelta(days=weeks_until_exhaustion * 7)).date()

        future_usage = forecast_weekly * weeks_remaining
        end_balance = credits_remaining - future_usage
        total_at_end = total_used + future_usage
        pct_at_end = total_at_end / purchased if purchased > 0 else 0.0

        if end_balance < 0:
            forecast_status = "EXHAUSTION_RISK"
        elif end_balance <= 50_000:
            forecast_status = "ON_TARGET"
        elif end_balance <= 150_000:
            forecast_status = "MODERATE_UNDERUSE"
        else:
            forecast_status = "HIGH_UNDERUSE"

        return Forecast(
            operational_weeks=op_count,
            historical_avg_burn=historical_avg_burn,
            latest_week_burn=latest_week_burn,
            recent_average_burn=recent_avg_burn,
            historical_weight_used=weights.get("historical_weight", 0),
            latest_week_weight_used=weights.get("latest_week_weight", 0),
            recent_average_weight_used=weights.get("recent_average_weight", 0),
            forecast_weekly_burn=forecast_weekly,
            forecast_monthly_burn=forecast_monthly,
            credits_remaining=credits_remaining,
            weeks_remaining=weeks_remaining,
            weeks_until_exhaustion=weeks_until_exhaustion,
            forecast_exhaustion_date=exhaustion_date,
            contract_end_date=pd.to_datetime(cs["contract_end_date"]).date(),
            forecast_future_usage_to_contract_end=future_usage,
            forecast_contract_end_balance=end_balance,
            forecast_total_contract_usage=total_at_end,
            forecast_percent_credits_used_by_contract_end=pct_at_end,
            forecast_status=forecast_status,
        )

    _AUTOSAVE_MARKER = "_autosave_date.txt"

    def build_forecast_context(self, cs=None, fc=None):
        """Assemble the ForecastContext shared by every prediction model.

        Honors a windowed operational frame (``_forecast_op_df``) when one is
        set, so model inputs match what ``get_forecast`` saw. Pass an already
        computed ``cs``/``fc`` to avoid recomputing them.
        """
        import datetime as _dt
        from .prediction import ForecastContext

        cs = cs if cs is not None else self.get_contract_status()
        fc = fc if fc is not None else self.get_forecast()

        obs_op = self._forecast_op_df if self._forecast_op_df is not None else self.operational_df
        obs_parts = []
        for df in (obs_op, self.historical_df):
            if df is not None and not df.empty and "total_credits_used" in df.columns:
                obs_parts.append(df["total_credits_used"])
        observations = pd.concat(obs_parts) if obs_parts else pd.Series(dtype="float64")

        # Chronological in-contract weekly burns for trend models (LinearRegression).
        if obs_op is not None and not obs_op.empty and "total_credits_used" in obs_op.columns:
            weekly_series = (
                obs_op.sort_values("week_start")["total_credits_used"]
                if "week_start" in obs_op.columns else obs_op["total_credits_used"]
            )
        else:
            weekly_series = None

        raw_date = cs.get("latest_usage_date")
        latest_date = (
            raw_date if isinstance(raw_date, _dt.date)
            else _dt.date.fromisoformat(str(raw_date)[:10])
        )

        return ForecastContext(
            credits_remaining=float(cs["credits_remaining"]),
            weeks_remaining=float(cs["weeks_remaining"]),
            latest_usage_date=latest_date,
            purchased_credits=float(cs["purchased_credits"]),
            forecast_weekly_burn=float(fc["forecast_weekly_burn"]),
            observations=observations,
            weekly_series=weekly_series,
        )

    def _run_mc(self, cs: dict, fc: dict):
        """Run Monte Carlo and return the full PredictionResult."""
        from .prediction import MonteCarloModel
        ctx = self.build_forecast_context(cs, fc)
        runs = min(int(self.config.get("forecast", {}).get("monte_carlo_runs", 10000)), 20000)
        return MonteCarloModel(runs=runs).run(ctx)

    def _run_ml(self, cs: dict, fc: dict):
        """Run the linear-trend (ML) model and return the PredictionResult."""
        from .prediction import LinearRegressionModel
        return LinearRegressionModel().run(self.build_forecast_context(cs, fc))

    @staticmethod
    def _ts_to_filename(snapshot_ts: str) -> str:
        return snapshot_ts.replace(":", "-").replace("T", "_") + ".json"

    def _save_snapshot_series(
        self, processed_dir: Path, snapshot_ts: str, snapshot: dict, mc_result=None, ml_result=None
    ) -> None:
        """Write full time-series data for a snapshot to a companion JSON file."""
        import math
        from datetime import date as _date, timedelta as _td

        contract_start = pd.to_datetime(self.config["contract"]["contract_start_date"])
        purchased = float(snapshot.get("purchased_credits", 0))

        # Collect weekly burn series from both data sources
        weekly_series: list[dict] = []
        for df, date_col in (
            (self.historical_df, "period_start"),
            (self.operational_df, "week_start"),
        ):
            if df is None or df.empty or date_col not in df.columns:
                continue
            for _, row in df.iterrows():
                ws = row[date_col]
                weekly_series.append({
                    "week_start": str(ws.date() if hasattr(ws, "date") else ws),
                    "total_credits_used": round(float(row.get("total_credits_used", 0)), 2),
                    "in_contract": bool(pd.Timestamp(ws) >= contract_start),
                })
        weekly_series.sort(key=lambda x: x["week_start"])

        # Reconstruct actual credit burndown from contract start
        rem = purchased
        actual_burndown: list[dict] = []
        for w in weekly_series:
            if w["in_contract"]:
                rem = max(rem - w["total_credits_used"], 0.0)
                actual_burndown.append({"date": w["week_start"], "remaining": round(rem, 1)})

        snap_date = str(snapshot.get("snapshot_date", ""))
        credits_remaining = float(snapshot.get("credits_remaining", 0))

        # Anchor forecast to latest_usage_date so dates align with the chart's weekly axis.
        # latest_usage_date = week_end.max() of the data, which is where the chart's
        # projected section begins.  snap_date is merely the calendar date of the save.
        latest_data_date = str(snapshot.get("latest_usage_date") or snap_date)

        # Deterministic forecast forward from latest data date
        weekly_burn = float(snapshot.get("forecast_weekly_burn", 0))
        weeks_remaining = float(snapshot.get("weeks_remaining", 0))
        forecast_burndown: list[dict] = []
        if latest_data_date:
            forecast_burndown.append({"date": latest_data_date, "remaining": round(credits_remaining, 1)})
            try:
                base = _date.fromisoformat(latest_data_date)
                n = min(math.ceil(weeks_remaining) + 1, 260)
                rem_f = credits_remaining
                for i in range(1, n + 1):
                    d = base + _td(days=i * 7)
                    rem_f = max(rem_f - weekly_burn, 0.0)
                    forecast_burndown.append({"date": str(d), "remaining": round(rem_f, 1)})
                    if rem_f == 0.0:
                        break
            except (ValueError, TypeError):
                pass

        # MC time series
        mc_series: dict = {}
        if mc_result is not None:
            mc_series = {
                "p10": mc_result.p10 or [],
                "p50": mc_result.burndown or [],
                "p90": mc_result.p90 or [],
                "exhaustion_probability": mc_result.metadata.get("exhaustion_probability"),
            }

        # Linear-trend (ML) time series — burndown + residual bands + stats.
        ml_series: dict = {}
        if ml_result is not None:
            ml_series = {
                "p10": ml_result.p10 or [],
                "p50": ml_result.burndown or [],
                "p90": ml_result.p90 or [],
                "slope_per_week": ml_result.metadata.get("slope_credits_per_week"),
                "raw_slope_per_week": ml_result.metadata.get("raw_slope_credits_per_week"),
                "trend_weight": ml_result.metadata.get("trend_weight"),
                "r_squared": ml_result.metadata.get("r_squared"),
                "metadata": ml_result.metadata,
            }

        data = {
            "snapshot_ts": snapshot_ts,
            "snapshot_date": snap_date,
            "label": str(snapshot.get("label", "")),
            "weekly_series": weekly_series,
            "actual_burndown": actual_burndown,
            "forecast_burndown": forecast_burndown,
            "mc": mc_series,
            "ml": ml_series,
        }

        snapshots_dir = processed_dir / "snapshots"
        snapshots_dir.mkdir(exist_ok=True)
        path = snapshots_dir / self._ts_to_filename(snapshot_ts)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def save_to_dir(
        self,
        processed_dir: Path,
        *,
        once_per_day: bool = True,
        label: str = "",
        snapshot_ts: str = "",
        snapshot_date: str = "",
        skip_mc: bool = False,
        skip_ml: bool = False,
    ) -> None:
        processed_dir.mkdir(parents=True, exist_ok=True)

        today_str = str(date.today())
        _snap_date = snapshot_date or today_str

        if once_per_day and not snapshot_ts:
            marker = processed_dir / self._AUTOSAVE_MARKER
            if marker.exists() and marker.read_text().strip() == today_str:
                return

        cs = self.get_contract_status()
        fc = self.get_forecast()

        if not snapshot_ts:
            pd.DataFrame([cs]).to_csv(processed_dir / "contract_status_summary.csv", index=False)
            pd.DataFrame([fc]).to_csv(processed_dir / "forecast_summary.csv", index=False)

        mc_result = None
        mc_stats: dict = {}
        if not skip_mc:
            try:
                mc_result = self._run_mc(cs, fc)
                mc_stats = {
                    "mc_runs": mc_result.metadata.get("runs"),
                    "mc_exhaustion_prob": mc_result.metadata.get("exhaustion_probability"),
                    "mc_p10_end_balance": round(mc_result.p10[-1]["value"], 1) if mc_result.p10 else None,
                    "mc_p50_end_balance": round(mc_result.burndown[-1]["value"], 1) if mc_result.burndown else None,
                    "mc_p90_end_balance": round(mc_result.p90[-1]["value"], 1) if mc_result.p90 else None,
                }
            except Exception:
                pass

        # Linear-trend (ML) projection — cheap, so generated by default.
        ml_result = None
        ml_stats: dict = {}
        if not skip_ml:
            try:
                ml_result = self._run_ml(cs, fc)
                md = ml_result.metadata or {}
                ml_stats = {
                    "ml_model_version": md.get("model_version"),
                    "ml_model_engine": md.get("model_engine"),
                    "ml_model_quality": md.get("model_quality"),
                    "ml_trend_direction": md.get("trend_direction"),
                    "ml_observations_used": md.get("observations_used"),
                    "ml_slope_per_week": md.get("slope_credits_per_week"),
                    "ml_raw_slope_per_week": md.get("raw_slope_credits_per_week"),
                    "ml_trend_weight": md.get("trend_weight"),
                    "ml_intercept": md.get("intercept"),
                    "ml_r_squared": md.get("r_squared"),
                    "ml_rmse": md.get("rmse"),
                    "ml_mae": md.get("mae"),
                    "ml_residual_std": md.get("residual_std"),
                    "ml_projected_exhaustion": md.get("projected_exhaustion"),
                    "ml_projected_exhaustion_date": md.get("projected_exhaustion_date"),
                    "ml_p10_end_balance": round(ml_result.p10[-1]["value"], 1) if ml_result.p10 else md.get("p10_end_balance"),
                    "ml_p50_end_balance": round(ml_result.burndown[-1]["value"], 1) if ml_result.burndown else md.get("p50_end_balance"),
                    "ml_p90_end_balance": round(ml_result.p90[-1]["value"], 1) if ml_result.p90 else md.get("p90_end_balance"),
                }
            except Exception:
                pass

        _snap_ts = snapshot_ts or datetime.now().isoformat(timespec="seconds")
        snapshot = {
            "snapshot_date": _snap_date,
            "snapshot_ts": _snap_ts,
            "label": label,
            **{k: str(v) if hasattr(v, "strftime") else v for k, v in {**cs, **fc}.items()},
            **mc_stats,
            **ml_stats,
        }

        history_path = processed_dir / "forecast_history.csv"
        if history_path.exists():
            existing = pd.read_csv(history_path, dtype=str, keep_default_na=False)
            combined = pd.concat([existing, pd.DataFrame([snapshot])], ignore_index=True)
        else:
            combined = pd.DataFrame([snapshot])

        combined.sort_values("snapshot_date").to_csv(history_path, index=False)

        try:
            self._save_snapshot_series(processed_dir, _snap_ts, snapshot, mc_result, ml_result)
        except Exception:
            pass

        if once_per_day and not snapshot_ts:
            (processed_dir / self._AUTOSAVE_MARKER).write_text(today_str)

    def get_weekly_chart_data(self) -> list[dict[str, Any]]:
        contract_start = pd.to_datetime(self.config["contract"]["contract_start_date"])
        rows: list[dict[str, Any]] = []

        if self.historical_df is not None and not self.historical_df.empty:
            for _, row in self.historical_df.iterrows():
                week_start = row["period_start"]
                period_end = row["period_end"]
                rows.append({
                    "week_start": str(week_start.date()),
                    "week_end": str(period_end.date()),
                    "total_credits_used": round(float(row["total_credits_used"]), 2),
                    "source": "historical",
                    "in_contract": week_start >= contract_start,
                })

        if self.operational_df is not None and not self.operational_df.empty:
            for _, row in self.operational_df.iterrows():
                week_start = row["week_start"]
                week_end = row["week_end"]
                rows.append({
                    "week_start": str(week_start.date()),
                    "week_end": str(week_end.date()),
                    "total_credits_used": round(float(row["total_credits_used"]), 2),
                    "source": "operational",
                    "in_contract": week_start >= contract_start,
                })

        rows.sort(key=lambda r: r["week_start"])
        return rows

    def _select_weights(self, op_count: int) -> dict[str, Any]:
        if self.config["forecast"].get("mode") == "auto":
            return self._select_auto_weights(op_count)
        fc = self.config["forecast"]
        return {
            "historical_weight": fc.get("historical_weight"),
            "recent_average_weight": fc.get("recent_average_weight"),
            "latest_week_weight": fc.get("latest_week_weight"),
        }

    def _select_auto_weights(self, op_count: int) -> dict[str, Any]:
        for rule in self.config["forecast"]["auto_weight_schedule"]:
            min_w = rule["min_operational_weeks"]
            max_w = rule.get("max_operational_weeks")
            if op_count >= min_w and (max_w is None or op_count <= max_w):
                return {
                    "historical_weight": rule.get("historical_weight"),
                    "recent_average_weight": rule.get("recent_average_weight"),
                    "latest_week_weight": rule.get("latest_week_weight"),
                }
        raise ValueError(f"No auto weight rule matched {op_count} operational weeks.")


class ChartDataBuilder:
    """Centralises all chart-data preparation. 
    Instantiate with a ForecastingService."""

    def __init__(
        self,
        forecasting_svc: ForecastingService | None = None,
        historical_df: pd.DataFrame | None = None,
        operational_df: pd.DataFrame | None = None,
    ) -> None:
        self._svc = forecasting_svc
        self._hist = historical_df
        self._op = operational_df

    def weekly_burn(self) -> list[dict[str, Any]]:
        if self._svc is None:
            return []
        return self._svc.get_weekly_chart_data()

    def weekly_burn_json(self) -> str:
        return json.dumps(self.weekly_burn())

    def burndown(self, contract_status: dict, forecast: dict) -> dict[str, Any]:
        purchased = contract_status["purchased_credits"]
        remaining = forecast["credits_remaining"]
        weekly_burn = forecast["forecast_weekly_burn"]
        weeks_left = forecast["weeks_remaining"]
        latest_date = str(contract_status["latest_usage_date"])

        ic = [w for w in self.weekly_burn() if w.get("in_contract")]
        ic.sort(key=lambda w: w["week_start"])

        r = purchased
        actual: list[tuple[str, float]] = []
        for w in ic:
            r = max(r - w["total_credits_used"], 0)
            actual.append((w["week_start"], r))
        actual.append((latest_date, remaining))

        proj: list[tuple[str, float]] = [(latest_date, remaining)]
        base = datetime.strptime(latest_date, "%Y-%m-%d")
        for i in range(1, min(int(weeks_left) + 2, 61)):
            d = base + timedelta(days=i * 7)
            rem = max(remaining - weekly_burn * i, 0)
            proj.append((d.strftime("%Y-%m-%d"), rem))
            if rem == 0:
                break

        all_labels = sorted({p[0] for p in actual + proj})
        return {"actual": actual, "proj": proj, "labels": all_labels, "purchased": purchased}

    def burndown_json(self, contract_status: dict, forecast: dict) -> str:
        return json.dumps(self.burndown(contract_status, forecast))

    def cumulative_burn(self) -> list[dict[str, Any]]:
        rows = sorted(self.weekly_burn(), key=lambda r: r["week_start"])
        total = 0.0
        out = []
        for r in rows:
            total += r["total_credits_used"]
            out.append({
                "week_start": r["week_start"],
                "cumulative": round(total, 2),
                "in_contract": r.get("in_contract", False),
            })
        return out

    def cumulative_burn_json(self) -> str:
        return json.dumps(self.cumulative_burn())

    def active_users_weekly(self, contract_start: str = "") -> list[dict[str, Any]]:
        rows: list[dict] = []
        if self._hist is not None and not self._hist.empty:
            for _, r in self._hist.iterrows():
                rows.append({
                    "week_start": str(r["period_start"].date()),
                    "active_users": int(r.get("credit_active_users") or r.get("unique_users") or 0),
                    "in_contract": False,
                })
        if self._op is not None and not self._op.empty:
            for _, r in self._op.iterrows():
                ws = str(r["week_start"].date())
                rows.append({
                    "week_start": ws,
                    "active_users": int(r.get("credit_active_users") or r.get("unique_users") or 0),
                    "in_contract": ws >= contract_start if contract_start else False,
                })
        rows.sort(key=lambda r: r["week_start"])
        return rows

    def active_users_json(self, contract_start: str = "") -> str:
        return json.dumps(self.active_users_weekly(contract_start))
