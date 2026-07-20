"""Shared "what's the true latest data" / "anchor a live forecast" logic.

Both the Forecast page (app/forecast/routes.py) and every other live
consumer of ForecastingService — the Summary page's KPI card, the
exhaustion-risk alert, diagnostics (all via app/shared/services.py's
build_forecasting_service) — need to agree on the same answer to "what's
actually the latest data we have" and "should a still-in-progress week
count toward the burn rate." A divergent answer here is exactly what made
credits_remaining disagree between the Forecast page's weekly/daily toggle
before that was fixed; this module is the one place every caller now goes
through instead of re-deriving it.
"""
from __future__ import annotations

import pandas as pd

from app.forecast.service import ForecastingService


def latest_data_date(*frames: tuple[pd.DataFrame | None, str]) -> pd.Timestamp:
    """Newest date actually present across the given (df, date_column) pairs."""
    candidates: list[pd.Timestamp] = []
    for df, col in frames:
        if df is None or df.empty or col not in df.columns:
            continue
        dates = pd.to_datetime(df[col], errors="coerce").dropna()
        if not dates.empty:
            candidates.append(dates.max().normalize())
    return max(candidates) if candidates else pd.Timestamp("today").normalize()


def exclude_incomplete_week_from_burn(svc: ForecastingService, as_of: pd.Timestamp) -> None:
    """Keep a still-in-progress week out of the burn-RATE window (its
    partial total would understate the weekly pace) without dropping it
    from `operational_df` itself — credits_remaining/the credit total must
    always count every day of usage that's actually happened, complete
    week or not."""
    if svc.operational_df is not None and not svc.operational_df.empty:
        complete_weeks = svc.operational_df[svc.operational_df["week_end"] <= as_of]
        if not complete_weeks.empty:
            svc._forecast_op_df = complete_weeks.copy()


def anchor_live_forecast(svc: ForecastingService, data_as_of: pd.Timestamp) -> None:
    """Anchor a ForecastingService to "now": lag-aware as-of/today (uploads
    lag the calendar, but ledger entries through today are already known),
    plus the partial-week burn exclusion above."""
    svc._as_of = data_as_of
    svc._today = pd.Timestamp.today().normalize()
    exclude_incomplete_week_from_burn(svc, data_as_of)
