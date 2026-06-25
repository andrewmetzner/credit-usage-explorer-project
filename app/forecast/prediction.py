"""
Prediction models for credit burn forecasting.

To add a new model:
  1. Subclass PredictionModel and set model_id, label, description.
  2. Implement run(ctx: ForecastContext) -> PredictionResult.
  3. Add the class to REGISTRY at the bottom of this file.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd


# Input/Output Types

@dataclass
class ForecastContext:
    """Common input snapshot fed to every prediction model."""
    credits_remaining: float
    weeks_remaining: float
    latest_usage_date: date
    purchased_credits: float
    forecast_weekly_burn: float   # deterministic centre value
    observations: pd.Series       # weekly total_credits_used (used for volatility)
    # Chronological in-contract weekly burns (for trend models); falls back to
    # `observations` when not supplied.
    weekly_series: pd.Series | None = None


@dataclass
class PredictionResult:
    """Standardised output from any prediction model."""
    model_id: str
    label: str
    # Primary burndown series — point estimate or P50
    burndown: list[dict]           # [{"date": "YYYY-MM-DD", "value": float}, ...]
    # Probability bands; None for deterministic models
    p10: list[dict] | None = None  # pessimistic edge (10th pct of remaining credits)
    p90: list[dict] | None = None  # optimistic edge (90th pct of remaining credits)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "label": self.label,
            "burndown": self.burndown,
            "p10": self.p10,
            "p90": self.p90,
            "metadata": self.metadata,
        }


# Abstract Base Class

class PredictionModel(ABC):
    """Abstract base for all forecast models."""
    model_id: str = ""
    label: str = ""
    description: str = ""

    @abstractmethod
    def run(self, ctx: ForecastContext) -> PredictionResult:
        """Execute the model and return a standardised result."""
        ...

    @classmethod
    def info(cls) -> dict:
        return {
            "model_id": cls.model_id,
            "label": cls.label,
            "description": cls.description,
        }


# Concrete Model Implementations

class DeterministicModel(PredictionModel):
    """Weighted-average deterministic burndown (wraps the base ForecastingService output)."""
    model_id = "deterministic"
    label = "Base Forecast"
    description = "Weighted average of historical, recent, and latest weekly burn."

    def run(self, ctx: ForecastContext) -> PredictionResult:
        pts = self._project(
            ctx.latest_usage_date,
            ctx.credits_remaining,
            ctx.forecast_weekly_burn,
            ctx.weeks_remaining,
        )
        return PredictionResult(
            model_id=self.model_id,
            label=self.label,
            burndown=pts,
            metadata={"weekly_burn": ctx.forecast_weekly_burn},
        )

    @staticmethod
    def _project(start: date, remaining: float, weekly_burn: float, weeks: float) -> list[dict]:
        pts = [{"date": str(start), "value": remaining}]
        n = min(math.ceil(weeks) + 1, 260)
        for i in range(1, n + 1):
            d = start + timedelta(days=i * 7)
            rem = max(remaining - weekly_burn * i, 0.0)
            pts.append({"date": str(d), "value": rem})
            if rem == 0.0:
                break
        return pts


class MonteCarloModel(PredictionModel):
    """Stochastic simulation that samples from empirical weekly burn volatility."""
    model_id = "monte_carlo"
    label = "Monte Carlo"
    description = "Samples empirical burn multipliers to produce P10/P50/P90 confidence bands."

    def __init__(self, runs: int = 1000, random_seed: int | None = 42):
        self.runs = runs
        self.random_seed = random_seed

    def run(self, ctx: ForecastContext) -> PredictionResult:
        multipliers = self._build_multipliers(ctx.observations)
        rng = np.random.default_rng(self.random_seed)

        full_weeks = int(math.floor(ctx.weeks_remaining))
        partial = ctx.weeks_remaining - full_weeks
        fracs = np.array([1.0] * full_weeks + ([partial] if partial > 1e-6 else []))
        n_steps = len(fracs)

        if n_steps == 0:
            single = [{"date": str(ctx.latest_usage_date), "value": ctx.credits_remaining}]
            return PredictionResult(
                model_id=self.model_id, label=self.label,
                burndown=single, p10=single, p90=single,
                metadata={"runs": self.runs, "exhaustion_probability": 0.0, "observations_used": 0},
            )

        # (runs x n_steps) - fully vectorized
        mults = rng.choice(multipliers, size=(self.runs, n_steps), replace=True)
        period_burns = np.maximum(ctx.forecast_weekly_burn * mults, 0.0) * fracs
        cum = np.cumsum(period_burns, axis=1)
        remaining = np.maximum(ctx.credits_remaining - cum, 0.0)

        # Prepend the start point (latest_usage_date -> credits_remaining)
        start_col = np.full((self.runs, 1), ctx.credits_remaining)
        all_rem = np.hstack([start_col, remaining])   # (runs × steps+1)

        dates = [
            str(ctx.latest_usage_date + timedelta(days=i * 7))
            for i in range(n_steps + 1)
        ]

        p10 = np.percentile(all_rem, 10, axis=0)
        p50 = np.percentile(all_rem, 50, axis=0)
        p90 = np.percentile(all_rem, 90, axis=0)
        exhaustion_prob = float(np.mean(remaining[:, -1] <= 0))

        def pts(arr: np.ndarray) -> list[dict]:
            return [{"date": d, "value": round(float(v), 1)} for d, v in zip(dates, arr)]

        return PredictionResult(
            model_id=self.model_id,
            label=self.label,
            burndown=pts(p50),
            p10=pts(p10),
            p90=pts(p90),
            metadata={
                "runs": self.runs,
                "exhaustion_probability": round(exhaustion_prob, 4),
                "observations_used": int(len(multipliers)),
                "random_seed": self.random_seed,
            },
        )

    @staticmethod
    def _build_multipliers(observations: pd.Series) -> np.ndarray:
        """Return an array of relative burn multipliers centred on the mean."""
        clean = pd.to_numeric(observations, errors="coerce").dropna()
        clean = clean[clean >= 0]
        if len(clean) < 2 or clean.mean() <= 0:
            return np.array([1.0])
        mults = (clean / clean.mean()).replace([np.inf, -np.inf], np.nan).dropna()
        arr = mults.to_numpy(dtype=float)
        return arr if len(arr) > 0 else np.array([1.0])


class LinearRegressionModel(PredictionModel):
    """Linear regression on the weekly-burn trend.

    The preferred engine is scikit-learn when it is installed.  A NumPy least-
    squares fallback is used automatically so the forecast page still returns
    ML statistics and chart overlays in lightweight deployments.
    """
    model_id = "linear_regression"
    label = "Linear Trend (ML)"
    description = "Linear regression on weekly burn, projected forward with residual bands."

    _Z_80 = 1.2816  # ~P10/P90 for a normal distribution

    def run(self, ctx: ForecastContext) -> PredictionResult:
        src = ctx.weekly_series if ctx.weekly_series is not None else ctx.observations
        y = pd.to_numeric(pd.Series(src), errors="coerce").dropna()
        y = y[y >= 0].to_numpy(dtype=float)

        full_weeks = int(math.floor(ctx.weeks_remaining))
        partial = ctx.weeks_remaining - full_weeks
        fracs = [1.0] * full_weeks + ([partial] if partial > 1e-6 else [])
        n_steps = len(fracs)

        if len(y) < 2 or n_steps == 0:
            return self._flat_result(ctx, int(len(y)), n_steps == 0)

        slope, intercept, fitted, engine = self._fit_line(y)
        residuals = y - fitted
        resid_std = float(np.std(residuals, ddof=1)) if len(y) > 2 else 0.0
        rmse = float(math.sqrt(np.mean(np.square(residuals)))) if len(y) else 0.0
        mae = float(np.mean(np.abs(residuals))) if len(y) else 0.0
        r2 = self._r_squared(y, fitted)

        mean_burn = float(np.mean(y)) if len(y) else 0.0
        median_burn = float(np.median(y)) if len(y) else 0.0
        center_burn = max(float(ctx.forecast_weekly_burn), mean_burn, median_burn, 0.0)

        # Stabilize the regression projection.  Plain linear extrapolation can
        # drive future weekly burn to zero or into an unrealistic hockey stick
        # when only a few noisy weeks are available.  We still fit/report the
        # linear model, but the projected burn is blended back toward the base
        # forecast unless the fit is strong and there is enough history.
        if len(y) < 4:
            trend_weight = 0.20
        elif r2 >= 0.70:
            trend_weight = 0.70
        elif r2 >= 0.35:
            trend_weight = 0.50
        else:
            trend_weight = 0.25
        if mean_burn > 0 and abs(slope) > mean_burn * 0.35:
            trend_weight *= 0.55

        burn_floor = center_burn * 0.18 if center_burn > 0 else 0.0
        burn_cap = max(center_burn * 2.25, float(np.max(y)) * 1.20 if len(y) else 0.0, burn_floor)
        effective_slope = float(slope) * float(trend_weight)

        dates = [str(ctx.latest_usage_date)]
        p50 = [round(float(ctx.credits_remaining), 1)]
        p10 = [round(float(ctx.credits_remaining), 1)]
        p90 = [round(float(ctx.credits_remaining), 1)]
        weekly_predictions: list[float] = []
        raw_weekly_predictions: list[float] = []
        remaining = float(ctx.credits_remaining)
        cum_var = 0.0
        last_idx = len(y) - 1
        exhaustion_date: str | None = None

        for k, frac in enumerate(fracs, start=1):
            raw_week_burn = float(intercept + slope * (last_idx + k))
            blended_week_burn = trend_weight * raw_week_burn + (1.0 - trend_weight) * float(ctx.forecast_weekly_burn)
            bounded_week_burn = min(max(blended_week_burn, burn_floor), burn_cap)
            week_burn = max(bounded_week_burn, 0.0) * frac
            raw_weekly_predictions.append(round(raw_week_burn * frac, 2))
            weekly_predictions.append(round(week_burn, 2))
            remaining = max(remaining - week_burn, 0.0)
            cum_var += (resid_std * frac) ** 2
            band = self._Z_80 * math.sqrt(cum_var)
            d = ctx.latest_usage_date + timedelta(days=k * 7)
            dates.append(str(d))
            p50.append(round(remaining, 1))
            p90.append(round(min(max(remaining + band, 0.0), ctx.purchased_credits), 1))
            p10.append(round(max(remaining - band, 0.0), 1))
            if remaining <= 0.0 and exhaustion_date is None:
                exhaustion_date = str(d)
                break

        def pts(vals: list[float]) -> list[dict]:
            return [{"date": d, "value": v} for d, v in zip(dates, vals)]

        slope_threshold = max(mean_burn * 0.02, 1.0)
        if effective_slope > slope_threshold:
            trend_direction = "increasing"
        elif effective_slope < -slope_threshold:
            trend_direction = "decreasing"
        else:
            trend_direction = "flat"

        if len(y) < 4:
            model_quality = "low_history"
        elif r2 >= 0.70:
            model_quality = "strong_fit"
        elif r2 >= 0.35:
            model_quality = "moderate_fit"
        else:
            model_quality = "weak_fit"

        exhausted = p50[-1] <= 0.0
        metadata = {
            "model_version": "stabilized_v2",
            "model_engine": engine,
            "slope_credits_per_week": round(float(effective_slope), 2),
            "raw_slope_credits_per_week": round(float(slope), 2),
            "intercept": round(float(intercept), 2),
            "trend_weight": round(float(trend_weight), 3),
            "burn_floor": round(float(burn_floor), 2),
            "burn_cap": round(float(burn_cap), 2),
            "r_squared": round(float(r2), 4),
            "residual_std": round(resid_std, 2),
            "rmse": round(rmse, 2),
            "mae": round(mae, 2),
            "observations_used": int(len(y)),
            "mean_weekly_burn": round(mean_burn, 2),
            "median_weekly_burn": round(median_burn, 2),
            "base_weekly_burn": round(float(ctx.forecast_weekly_burn), 2),
            "last_observed_burn": round(float(y[-1]), 2),
            "next_week_predicted_burn": weekly_predictions[0] if weekly_predictions else None,
            "next_week_raw_regression_burn": raw_weekly_predictions[0] if raw_weekly_predictions else None,
            "trend_direction": trend_direction,
            "model_quality": model_quality,
            "projected_exhaustion": bool(exhausted),
            "projected_exhaustion_date": exhaustion_date,
            "p10_end_balance": p10[-1] if p10 else None,
            "p50_end_balance": p50[-1] if p50 else None,
            "p90_end_balance": p90[-1] if p90 else None,
            "weekly_predictions": weekly_predictions[:26],
            "raw_weekly_predictions": raw_weekly_predictions[:26],
        }

        return PredictionResult(
            model_id=self.model_id,
            label=self.label,
            burndown=pts(p50),
            p10=pts(p10),
            p90=pts(p90),
            metadata=metadata,
        )

    @staticmethod
    def _fit_line(y: np.ndarray) -> tuple[float, float, np.ndarray, str]:
        """Return slope, intercept, fitted values, and engine name."""
        X = np.arange(len(y), dtype=float)
        try:
            from sklearn.linear_model import LinearRegression

            reg = LinearRegression().fit(X.reshape(-1, 1), y)
            fitted = reg.predict(X.reshape(-1, 1))
            return float(reg.coef_[0]), float(reg.intercept_), fitted.astype(float), "sklearn"
        except Exception:
            # NumPy fallback keeps the route working when scikit-learn is absent.
            A = np.vstack([X, np.ones(len(X))]).T
            slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
            fitted = slope * X + intercept
            return float(slope), float(intercept), fitted.astype(float), "numpy_lstsq"

    @staticmethod
    def _r_squared(y: np.ndarray, fitted: np.ndarray) -> float:
        ss_res = float(np.sum(np.square(y - fitted)))
        ss_tot = float(np.sum(np.square(y - np.mean(y))))
        if ss_tot <= 0:
            return 1.0 if ss_res <= 1e-9 else 0.0
        return 1.0 - ss_res / ss_tot

    def _flat_result(self, ctx: ForecastContext, observations_used: int, no_horizon: bool) -> PredictionResult:
        pts = DeterministicModel._project(
            ctx.latest_usage_date,
            ctx.credits_remaining,
            ctx.forecast_weekly_burn,
            ctx.weeks_remaining,
        )
        end_balance = pts[-1]["value"] if pts else ctx.credits_remaining
        return PredictionResult(
            model_id=self.model_id,
            label=self.label,
            burndown=pts,
            p10=pts,
            p90=pts,
            metadata={
                "insufficient_data": observations_used < 2,
                "no_projection_horizon": bool(no_horizon),
                "model_version": "stabilized_v2",
                "model_engine": "flat_fallback",
                "observations_used": int(observations_used),
                "slope_credits_per_week": 0.0,
                "intercept": round(float(ctx.forecast_weekly_burn), 2),
                "r_squared": None,
                "residual_std": 0.0,
                "rmse": None,
                "mae": None,
                "trend_direction": "flat",
                "model_quality": "insufficient_data" if observations_used < 2 else "no_horizon",
                "projected_exhaustion": bool(end_balance <= 0.0),
                "projected_exhaustion_date": next((p["date"] for p in pts if p["value"] <= 0.0), None),
                "p10_end_balance": end_balance,
                "p50_end_balance": end_balance,
                "p90_end_balance": end_balance,
            },
        )


# Model Registry and Factory

REGISTRY: dict[str, type[PredictionModel]] = {
    DeterministicModel.model_id: DeterministicModel,
    MonteCarloModel.model_id: MonteCarloModel,
    LinearRegressionModel.model_id: LinearRegressionModel,
}


def get_model(model_id: str, **kwargs: Any) -> PredictionModel:
    """Instantiate a registered model by ID; extra kwargs are forwarded to __init__."""
    cls = REGISTRY.get(model_id)
    if cls is None:
        raise ValueError(f"Unknown model {model_id!r}. Available: {sorted(REGISTRY)}")
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()
