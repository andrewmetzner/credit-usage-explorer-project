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


# Model Registry and Factory

REGISTRY: dict[str, type[PredictionModel]] = {
    DeterministicModel.model_id: DeterministicModel,
    MonteCarloModel.model_id: MonteCarloModel,
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
