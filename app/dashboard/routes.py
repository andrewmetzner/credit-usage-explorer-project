"""Dashboard blueprint (`main`): core pages + registration of the concern modules.

The setup wizard, diagnostics, outliers/alerts, and upload routes live in their
own modules and register onto this same `main` blueprint, so every existing
`url_for("main.*")` reference stays valid while each concern is editable in
isolation.
"""
from __future__ import annotations

from collections import OrderedDict
from urllib.parse import urlencode

import pandas as pd
from flask import Blueprint, redirect, render_template, request, url_for

from app.shared.chart_data import usage_type_weekly_json
from app.shared.csv_export import csv_response, labeled_export_df, range_slug
from app.shared.data_store import CreditUsageData
from .service import (
    DEFAULT_RECORD_COLUMNS,
    build_record_view,
    compute_active_users_weekly,
    compute_summary_metrics,
    compute_daily_trend,
    compute_weekly_trend,
    record_column_meta,
)
from .setup_routes import register_setup_routes
from .diagnostics_routes import register_diagnostics_routes
from .alerts_routes import register_alerts_routes
from .upload_routes import register_upload_routes

# Optional: same "safe to surface, never mutates" status check Settings uses
# (see openai_admin_API/status.py) — just for the Summary page's own
# "Sync now" button visibility, wrapped so a machine with no aauth/ key
# configured (or the package altogether missing) still starts fine.
try:
    from openai_admin_API.status import get_admin_api_status
except ImportError:
    def get_admin_api_status(force_refresh: bool = False) -> dict:
        return {"key_configured": False, "read_key_configured": False}


def create_dashboard_blueprint(services) -> Blueprint:
    store = services.store
    pipeline = services.pipeline
    config_svc = services.config_svc
    bp = Blueprint("main", __name__, template_folder="templates")

    def data() -> CreditUsageData:
        return store.data

    # Rendered records-tbody fragments, keyed by (dataset identity, query
    # string). A new upload creates a new CreditUsageData object, so stale
    # entries simply stop being hit and age out of the small LRU. Entries can
    # reach ~14 MB (all ~19k rows), hence the tight cap.
    _rows_html_cache: OrderedDict = OrderedDict()
    _ROWS_HTML_CACHE_MAX = 4
    # Rows rendered into the table by default. Rendering all ~25k rows was the
    # main per-request cost (Jinja building tens of thousands of <tr>, plus a
    # heavy browser DOM) — and it re-paid in full after every sync, since the
    # cache keys on the data version. Filters/sort still apply to the FULL
    # dataset; this only caps how many of the results get drawn. "show all"
    # (limit=0) and Export CSV remain uncapped.
    _DEFAULT_RECORDS_LIMIT = 1000

    def _records_query_state(d: CreditUsageData):
        search_field = request.args.get("search_field", "any")
        search_query = request.args.get("search_query", "").strip()
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        min_credits = request.args.get("min_credits", "").strip()
        max_credits = request.args.get("max_credits", "").strip()
        zero_credits = request.args.get("zero_credits", "")
        usage_type = request.args.get("usage_type", "").strip()
        model = request.args.get("model", "").strip()
        tier = request.args.get("tier", "").strip()
        lookback_days = request.args.get("lookback_days", "").strip()
        sort_by = request.args.get("sort_by", "").strip()
        sort_order = request.args.get("sort_order", "asc").strip()
        if sort_order not in {"asc", "desc"}:
            sort_order = "asc"

        # A lookback (from an alert deep-link) resolves to an explicit window off
        # the latest data date, so the date inputs show the range being viewed.
        if lookback_days and not (start_date or end_date) and "date_partition" in d.df.columns:
            dts = pd.to_datetime(d.df["date_partition"], errors="coerce")
            if dts.notna().any() and lookback_days.isdigit():
                days = max(int(lookback_days), 1)
                start_date = str((dts.max() - pd.Timedelta(days=days - 1)).date())
                end_date = str(dts.max().date())

        # "tier" is a synthetic, config-derived column (not part of the raw
        # data), so it's valid as a display/search field alongside d.columns.
        available_columns = list(d.columns) + ["tier"]
        selected_param = [c for c in request.args.getlist("selected_fields") if c in available_columns]
        selected_fields = (
            [c for c in available_columns if c in selected_param] if selected_param
            else [c for c in DEFAULT_RECORD_COLUMNS if c in d.columns]
        )

        df = d.df.copy()
        df = d.filter_by_date(df, start_date, end_date)
        df = d.filter_by_credits(df, min_credits, max_credits, zero_credits)
        if "email" in df.columns:
            df["tier"] = services.governance.tier_column(df)
        # Usage-type filter runs on the corrected parsed type, so "codex" also
        # catches API rows that arrive labeled as chat.
        if usage_type and "usage_type_parsed_type" in df.columns:
            df = df[df["usage_type_parsed_type"] == usage_type]
        if model and "usage_type_model" in df.columns:
            df = df[df["usage_type_model"] == model]
        if tier and "tier" in df.columns:
            df = df[df["tier"] == tier]

        if search_query:
            if search_field == "any":
                mask = pd.Series(False, index=df.index)
                for col in df.columns:
                    mask |= df[col].astype(str).str.contains(
                        search_query, case=False, na=False, regex=False
                    )
                df = df[mask]
            elif search_field in df.columns:
                df = df[
                    df[search_field].astype(str).str.contains(
                        search_query, case=False, na=False, regex=False
                    )
                ]

        if sort_by in df.columns:
            sort_key = pd.to_numeric(df[sort_by], errors="coerce")
            if sort_key.notna().any():
                df = df.assign(_sort_key=sort_key).sort_values(
                    "_sort_key", ascending=(sort_order == "asc"), na_position="last"
                ).drop(columns="_sort_key")
            else:
                df = df.sort_values(
                    sort_by, ascending=(sort_order == "asc"), na_position="last",
                    key=lambda s: s.astype(str).str.lower()
                )

        return {
            "df": df,
            "selected_fields": selected_fields,
            "search_field": search_field,
            "search_query": search_query,
            "start_date": start_date,
            "end_date": end_date,
            "min_credits": min_credits,
            "max_credits": max_credits,
            "zero_credits": zero_credits,
            "usage_type": usage_type,
            "model": model,
            "tier": tier,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }

    def _query_url(endpoint: str, **overrides) -> str:
        pairs = []
        args = request.args.copy()
        for key, value in overrides.items():
            args.pop(key, None)
            if value not in (None, ""):
                if isinstance(value, (list, tuple)):
                    for item in value:
                        pairs.append((key, item))
                else:
                    pairs.append((key, value))
        for key in args:
            for value in args.getlist(key):
                if value:
                    pairs.append((key, value))
        qs = urlencode(pairs)
        return f"{url_for(endpoint)}?{qs}" if qs else url_for(endpoint)

    @bp.route("/data-revision", methods=["GET"])
    def data_revision() -> object:
        """Lightweight polling target for the open-page new-data notification
        (see the poller in base.html): returns the in-memory data's revision
        counter, bumped every time the store reloads (e.g. after an API sync),
        plus a short human summary of what the most recent sync loaded. When a
        page's captured revision no longer matches, it shows a "new data
        loaded — reload" prompt (rather than force-reloading) so the user
        chooses when to refresh."""
        info = ""
        try:
            from openai_admin_API.scheduler import load_sync_config

            cfg = load_sync_config()
            rows = int(cfg.get("last_run_rows") or 0)
            credits = float(cfg.get("last_run_credits") or 0)
            if rows:
                info = f"+{rows:,} row{'s' if rows != 1 else ''}"
                if credits:
                    info += f", +{credits:,.0f} credits"
        except Exception:
            pass
        return {"revision": store.revision, "info": info}

    @bp.route("/", methods=["GET"])
    def index() -> str:
        return redirect(url_for("main.summary_page"))

    @bp.route("/summary", methods=["GET"])
    def summary_page() -> str:
        from config import DEFAULT_DATA_PATH

        d = data()
        df = d.df

        # The ACTIVE contract's own start still drives the Total Credits Spent
        # metric's "in contract" sub-line (a single-contract figure). The bar
        # graphs below use the FULL contract list instead, so each week/day
        # can be scoped to whichever specific contract(s) it falls into.
        from app.shared.contracts import sort_contracts

        contracts = sort_contracts(config_svc.load_contracts())
        contract_start_str = ""
        try:
            contract_start_str = str(
                config_svc.load_contract().get("contract", {}).get("contract_start_date", "") or ""
            )
        except Exception:
            contract_start_str = ""

        metrics = compute_summary_metrics(df, contract_start_str)

        weekly_trend = compute_weekly_trend(df, contracts)
        daily_trend = compute_daily_trend(df, contracts)
        # All three Summary charts share one raw-frame week grouping + contract
        # split, so they always cover the same weeks (no straddling-week gap).
        active_users_data = compute_active_users_weekly(df, contracts)

        forecast_snapshot = None
        ps = pipeline.status()
        try:
            from app.shared.contracts import resolve_contract_config

            config = resolve_contract_config(config_svc, pipeline=pipeline)
            svc = services.build_forecasting_service(config)
            if svc.has_data():
                cs = svc.get_contract_status()
                fc = svc.get_forecast()
                forecast_snapshot = {
                    "pacing_status": cs["pacing_status"],
                    "burn_pace_ratio": cs["burn_pace_ratio"],
                    "credits_remaining": cs["credits_remaining"],
                    "percent_credits_used": cs["percent_credits_used"],
                    "percent_contract_elapsed": cs["percent_contract_elapsed"],
                    "weeks_remaining": cs["weeks_remaining"],
                    "forecast_status": fc["forecast_status"],
                    "forecast_weekly_burn": fc["forecast_weekly_burn"],
                    "forecast_contract_end_balance": fc["forecast_contract_end_balance"],
                }
        except Exception:
            pass

        return render_template(
            "summary.html",
            metrics=metrics,
            weekly_trend=weekly_trend,
            daily_trend=daily_trend,
            usage_type_weekly=usage_type_weekly_json(df, contracts=contracts),
            forecast_snapshot=forecast_snapshot,
            pipeline_status=ps,
            admin_api_status=get_admin_api_status(),
            data_source={
                "filename": None if store.path == DEFAULT_DATA_PATH else store.path.name,
                "rows": metrics["total_records"],
            },
            active_users_data=active_users_data,
            contracts=[
                {"id": c.get("id"), "label": str(c.get("label") or "Contract")}
                for c in contracts
            ],
        )

    @bp.route("/records", methods=["GET"])
    def records_page() -> str:
        d = data()
        state = _records_query_state(d)
        df = state["df"]
        selected_fields = state["selected_fields"]
        # Capped to _DEFAULT_RECORDS_LIMIT rows by default (link to show more /
        # all sits next to the record count); ?limit=0 renders everything.
        # Filters/sort apply to the whole dataset regardless — the cap only
        # limits how many result rows are drawn. Export CSV is never capped.
        try:
            render_limit = int(request.args.get("limit", _DEFAULT_RECORDS_LIMIT))
        except (TypeError, ValueError):
            render_limit = _DEFAULT_RECORDS_LIMIT

        # Keyed on the data's file-signature revision (stable across reloads of
        # the same file, and across a restart) rather than id(d), which changes
        # on every reload and could even be reused by a later object.
        cache_key = (store.revision, request.query_string.decode())
        cached = _rows_html_cache.get(cache_key)
        if cached is None:
            capped_df = df.head(render_limit) if render_limit > 0 else df
            columns, rows = build_record_view(capped_df, selected_fields)
            rows_html = render_template("records_rows.html", rows=rows, columns=columns)
            cached = (rows_html, len(rows), columns)
            _rows_html_cache[cache_key] = cached
            while len(_rows_html_cache) > _ROWS_HTML_CACHE_MAX:
                _rows_html_cache.popitem(last=False)
        else:
            _rows_html_cache.move_to_end(cache_key)
        rows_html, rows_shown, columns = cached

        def _options(col: str) -> list[str]:
            return (
                sorted(d.df[col].dropna().astype(str).unique().tolist())
                if col in d.df.columns else []
            )

        # Tier options reflect every tier actually in use across the whole
        # dataset (not just the currently filtered rows), same as usage
        # type/model above.
        tier_options = services.governance.tier_options(d.df)

        return render_template(
            "index.html",
            toggle_columns=[record_column_meta(c) for c in d.columns] + [record_column_meta("tier")],
            columns=columns,
            rows_html=rows_html,
            rows_shown=rows_shown,
            row_count=len(df),
            selected_fields=set(selected_fields),
            headers=list(d.columns) + ["tier"],
            search_field=state["search_field"],
            search_query=state["search_query"],
            start_date=state["start_date"],
            end_date=state["end_date"],
            min_credits=state["min_credits"],
            max_credits=state["max_credits"],
            zero_credits=state["zero_credits"],
            usage_type=state["usage_type"],
            model=state["model"],
            tier=state["tier"],
            sort_by=state["sort_by"],
            sort_order=state["sort_order"],
            export_url=_query_url("main.records_export_csv"),
            limit_options=[
                (n, _query_url("main.records_page", limit=n))
                for n in (1000, 5000, 10000)
            ] + [(0, _query_url("main.records_page", limit=0))],
            usage_type_options=_options("usage_type_parsed_type"),
            model_options=_options("usage_type_model"),
            tier_options=tier_options,
        )

    @bp.route("/records/export.csv", methods=["GET"])
    def records_export_csv() -> object:
        d = data()
        state = _records_query_state(d)
        columns, rows = build_record_view(state["df"], state["selected_fields"])
        search = (
            f"{state['search_field']}_{state['search_query']}"
            if state["search_query"] else ""
        )
        sort = f"{state['sort_by']}_{state['sort_order']}" if state["sort_by"] else ""
        return csv_response(labeled_export_df(rows, columns), "records.csv", filters=[
            ("type", state["usage_type"]),
            ("model", state["model"]),
            ("dates", range_slug(state["start_date"], state["end_date"])),
            ("search", search),
            ("credits", range_slug(state["min_credits"], state["max_credits"], "0", "max")),
            ("zero", "only" if state["zero_credits"] == "1" else ""),
            ("sort", sort),
        ])

    # ── Concern modules register their routes onto this same blueprint ──
    register_setup_routes(bp, services)
    register_diagnostics_routes(bp, services)
    register_alerts_routes(bp, services)
    register_upload_routes(bp, services)

    return bp
