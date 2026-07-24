"""Application service container.

Bundles the app-wide singletons (data store, ingestion pipeline, config) and
the small factories that build on them, so blueprints and helpers depend on one
object instead of threading three arguments around in varying orders.

Add a new shared dependency or derived service here once, and every caller gets
it for free.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycles at runtime
    import pandas as pd

    from app.forecast.service import ForecastingService
    from .config_service import AppConfig
    from .data_store import DataStore
    from .ingestion import IngestionPipeline


class Services:
    """Holds the shared singletons and convenience builders."""

    def __init__(self, store: "DataStore", pipeline: "IngestionPipeline", config_svc: "AppConfig") -> None:
        self.store = store
        self.pipeline = pipeline
        self.config_svc = config_svc

    @property
    def df(self) -> "pd.DataFrame":
        """The currently loaded usage dataframe."""
        return self.store.data.df

    @property
    def governance(self):
        """A fresh per-request GovernanceService (memoizes within itself, so
        grab it once per request and reuse; the next request sees fresh config)."""
        from .governance import GovernanceService

        return GovernanceService(self.config_svc)

    def build_forecasting_service(
        self, config: dict | None = None, *, daily_fallback: bool = True, anchor: bool = True
    ) -> "ForecastingService":
        """Construct a ForecastingService from the pipeline summaries.

        Falls back to deriving weekly data from the daily store frame when no
        pipeline (historical/operational) summaries exist. This is the one place
        that construction lives, instead of being copy-pasted across routes.

        anchor=True (the default) additionally applies the same lag-aware
        "now" anchoring the live Forecast page uses (see
        forecast_window.anchor_live_forecast) — needed by any caller whose
        numbers should agree with the Forecast page's (dashboard KPI card,
        exhaustion alert, diagnostics). Pass anchor=False for callers that
        need ForecastingService's own un-nudged computation instead — e.g.
        the analytics storyboard's "live forecast" timeline marker — where
        the goal is only removing the duplicated construction, not shifting
        already-established numbers. The un-anchored path also passes the
        daily frame through unconditionally rather than gating it on both
        historical/operational being absent — ForecastingService's own
        __init__ already only uses it to derive whichever one is missing,
        so this matches exactly what those callers' inlined construction
        did before, without the anchored path's narrower gate.
        """
        from app.forecast.service import ForecastingService

        if config is None:
            from .contracts import resolve_contract_config

            config = resolve_contract_config(self.config_svc, pipeline=self.pipeline)
        hist_df = self.pipeline.get_historical_weekly_summary()
        op_df = self.pipeline.get_operational_weekly_summary()
        daily_df = self.store.data.df if daily_fallback else None
        # Passed through unconditionally now (not just when hist/op are both
        # absent) — ForecastingService's own _daily_already_included guard
        # prevents double-counting when it's used for derivation instead;
        # when weekly summaries already exist, this lets it extend
        # credits_remaining/latest_usage_date past the last complete week
        # using real already-known daily rows, so this page's numbers agree
        # with the Forecast page's daily chart instead of lagging a week
        # behind it.
        svc = ForecastingService(config, hist_df, op_df, daily_df)
        if anchor:
            from .forecast_window import anchor_live_forecast, latest_data_date

            # Same lag-aware anchoring the live Forecast page applies, so a
            # fresh upload can't leave this page's numbers (pacing, burn
            # rate, exhaustion date) disagreeing with the Forecast page's.
            data_as_of = latest_data_date(
                (daily_df, "date_partition"), (op_df, "week_end"), (hist_df, "period_end"),
            )
            anchor_live_forecast(svc, data_as_of)
        return svc
