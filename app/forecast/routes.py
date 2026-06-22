from __future__ import annotations

import copy
import datetime as _dt

import pandas as _pd
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from app.shared.config_service import AppConfig
from app.shared.data_store import DataStore
from app.shared.ingestion import IngestionPipeline
from .models import PriceModel
from .prediction import ForecastContext, get_model
from .service import ChartDataBuilder, ForecastingService


def create_forecast_blueprint(
    pipeline: IngestionPipeline,
    config_svc: AppConfig,
    store: DataStore | None = None,
) -> Blueprint:
    bp = Blueprint("forecast", __name__, template_folder="templates", url_prefix="")

    def _get_store_df():
        return store.data.df if store is not None else None

    def _build_forecast_context(template_name: str) -> str:
        cost_per_credit = float(request.args.get("cost_per_credit", 0) or 0)
        available_credits = float(request.args.get("available_credits", 0) or 0)
        total_credit_cost = float(request.args.get("total_credit_cost", 0) or 0)

        config = config_svc.load_contract()

        is_preview = False
        preview_keys = [
            "contract_start_date", "contract_end_date", "purchased_credits",
            "price_per_credit", "forecast_mode",
            "historical_weight", "latest_week_weight", "recent_average_weight",
        ]
        for key in preview_keys:
            if request.args.get(key):
                is_preview = True
                if key == "contract_start_date":
                    config["contract"]["contract_start_date"] = request.args.get(key)
                elif key == "contract_end_date":
                    config["contract"]["contract_end_date"] = request.args.get(key)
                elif key == "purchased_credits":
                    config["contract"]["purchased_credits"] = float(request.args.get(key))
                elif key == "price_per_credit":
                    config["pricing"]["current_price_per_credit"] = float(request.args.get(key))
                elif key == "forecast_mode":
                    config["forecast"]["mode"] = request.args.get(key)
                elif key in ("historical_weight", "latest_week_weight", "recent_average_weight"):
                    config["forecast"][key] = float(request.args.get(key))

        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None

        forecasting = ForecastingService(config, hist_df, op_df, daily_fallback)

        exclude_partial = request.args.get("exclude_partial") == "1"
        if exclude_partial:
            _today = _pd.Timestamp("today").normalize()
            if forecasting.operational_df is not None and not forecasting.operational_df.empty:
                forecasting.operational_df = forecasting.operational_df[
                    forecasting.operational_df["week_end"] < _today
                ].copy()
            forecasting._as_of = _today

        # Date window for burn-rate calculation (does not affect credits_remaining / weeks_remaining)
        data_from = request.args.get("data_from", "").strip() or None
        data_to   = request.args.get("data_to",   "").strip() or None
        if (data_from or data_to) and forecasting.operational_df is not None and not forecasting.operational_df.empty:
            win = forecasting.operational_df.copy()
            if data_from:
                win = win[win["week_start"] >= _pd.to_datetime(data_from)]
            if data_to:
                win = win[win["week_end"] <= _pd.to_datetime(data_to)]
            if not win.empty:
                forecasting._forecast_op_df = win

        if not forecasting.has_data():
            return render_template(
                template_name,
                no_data=True,
                price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
                contract_status=None,
                forecast=None,
                weekly_chart_data="[]",
                cumulative_chart_data="[]",
                active_users_data="[]",
                pipeline_status=pipeline.status(),
                forecast_history=[],
                is_preview=False,
                saved_contract=config_svc.load_contract(),
                exclude_partial=exclude_partial,
                data_from=data_from,
                data_to=data_to,
            )

        contract_status = forecasting.get_contract_status()
        forecast_data = forecasting.get_forecast()

        auto_save_mode = config.get("forecast", {}).get("snapshot_auto_save", "daily")
        if not is_preview and auto_save_mode in ("daily", "both"):
            forecasting.save_to_dir(pipeline.processed_dir)

        chart_builder = ChartDataBuilder(forecasting, forecasting.historical_df, forecasting.operational_df)
        weekly_chart_data = chart_builder.weekly_burn_json()
        cumulative_chart_data = chart_builder.cumulative_burn_json()
        contract_start_str = str(contract_status.get("contract_start_date", ""))
        active_users_data = chart_builder.active_users_json(contract_start_str)

        return render_template(
            template_name,
            no_data=False,
            price_model=PriceModel(cost_per_credit, available_credits, total_credit_cost),
            contract_status=contract_status,
            forecast=forecast_data,
            weekly_chart_data=weekly_chart_data,
            cumulative_chart_data=cumulative_chart_data,
            active_users_data=active_users_data,
            pipeline_status=pipeline.status(),
            forecast_history=pipeline.get_forecast_history(),
            is_preview=is_preview,
            saved_contract=config_svc.load_contract(),
            exclude_partial=exclude_partial,
            data_from=data_from,
            data_to=data_to,
        )

    @bp.route("/forecast", methods=["GET"])
    def forecast_page() -> str:
        if "exclude_partial" not in request.args:
            args = dict(request.args)
            saved = request.cookies.get("forecast_excl_partial", "1")
            args["exclude_partial"] = saved if saved in ("0", "1") else "1"
            return redirect(url_for("forecast.forecast_page", **args))
        return _build_forecast_context("forecast.html")

    @bp.route("/forecast/save-config", methods=["POST"])
    def save_forecast_config() -> object:
        contract = config_svc.load_contract()

        if request.form.get("contract_start_date"):
            contract["contract"]["contract_start_date"] = request.form.get("contract_start_date")
        if request.form.get("contract_end_date"):
            contract["contract"]["contract_end_date"] = request.form.get("contract_end_date")
        if request.form.get("purchased_credits"):
            contract["contract"]["purchased_credits"] = float(request.form.get("purchased_credits"))
        if request.form.get("rollover_allowed"):
            contract["contract"]["rollover_allowed"] = request.form.get("rollover_allowed") == "on"
        if request.form.get("current_price_per_credit"):
            contract["pricing"]["current_price_per_credit"] = float(request.form.get("current_price_per_credit"))
        if request.form.get("next_contract_price_per_credit"):
            contract["pricing"]["next_contract_price_per_credit"] = float(request.form.get("next_contract_price_per_credit"))
        if request.form.get("forecast_mode"):
            contract["forecast"]["mode"] = request.form.get("forecast_mode")
        if request.form.get("recent_average_window_weeks"):
            contract["forecast"]["recent_average_window_weeks"] = int(request.form.get("recent_average_window_weeks"))
        if request.form.get("minimum_weeks_for_recent_average"):
            contract["forecast"]["minimum_weeks_for_recent_average"] = int(request.form.get("minimum_weeks_for_recent_average"))
        if request.form.get("normalize_weights"):
            contract["forecast"]["normalize_weights"] = request.form.get("normalize_weights") == "on"

        for wkey in ("historical_weight", "latest_week_weight", "recent_average_weight"):
            if request.form.get(wkey):
                w = request.form.get(wkey)
                contract["forecast"][wkey] = float(w) if w else None

        if request.form.get("snapshot_auto_save"):
            contract["forecast"]["snapshot_auto_save"] = request.form.get("snapshot_auto_save")

        if request.form.get("monte_carlo_runs"):
            try:
                contract["forecast"]["monte_carlo_runs"] = max(100, min(20000, int(request.form.get("monte_carlo_runs"))))
            except (ValueError, TypeError):
                pass

        config_svc.save_contract(contract)
        flash("Forecast config saved.", "success")
        next_page = request.form.get("next_page", "settings")
        if next_page == "forecast":
            return redirect(url_for("forecast.forecast_page"))
        return redirect(url_for("settings.settings_page"))

    @bp.route("/forecast/snapshot-settings", methods=["POST"])
    def save_snapshot_settings() -> object:
        contract = config_svc.load_contract()
        contract["forecast"]["snapshot_auto_save"] = request.form.get("snapshot_auto_save", "daily")
        config_svc.save_contract(contract)
        flash("Snapshot auto-save setting updated.", "success")
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/history/delete", methods=["POST"])
    def delete_forecast_snapshot() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        label = request.form.get("label", "")
        ok, err = pipeline.delete_snapshot(snapshot_ts, snapshot_date, label)
        if not ok:
            return (err or "Delete failed.", 500)
        return ("", 204)

    @bp.route("/forecast/snapshot", methods=["POST"])
    def save_forecast_snapshot() -> object:
        config = copy.deepcopy(config_svc.load_contract())

        if request.form.get("snap_purchased_credits"):
            config["contract"]["purchased_credits"] = float(request.form["snap_purchased_credits"])
        if request.form.get("snap_contract_start_date"):
            config["contract"]["contract_start_date"] = request.form["snap_contract_start_date"]
        if request.form.get("snap_contract_end_date"):
            config["contract"]["contract_end_date"] = request.form["snap_contract_end_date"]
        if request.form.get("snap_forecast_mode"):
            config["forecast"]["mode"] = request.form["snap_forecast_mode"]
        for wkey in ("historical_weight", "recent_average_weight", "latest_week_weight"):
            val = request.form.get(f"snap_{wkey}", "").strip()
            if val:
                config["forecast"][wkey] = float(val)

        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None
        svc = ForecastingService(config, hist_df, op_df, daily_fallback)
        label = request.form.get("snapshot_label", "").strip()

        if svc.has_data():
            svc.save_to_dir(pipeline.processed_dir, once_per_day=False, label=label)
            flash("Snapshot saved.", "success")
        else:
            flash("No data available to snapshot.", "warning")

        next_page = request.form.get("next_page", "forecast")
        if next_page == "settings":
            return redirect(url_for("settings.settings_page"))
        return redirect(url_for("forecast.forecast_page"))

    @bp.route("/forecast/history/rename", methods=["POST"])
    def rename_forecast_snapshot() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        old_label = request.form.get("old_label", "")
        new_label = request.form.get("new_label", "").strip()
        ok, err = pipeline.rename_snapshot(snapshot_ts, snapshot_date, old_label, new_label)
        if not ok:
            return (err or "Rename failed.", 500)
        return ("", 204)

    @bp.route("/forecast/snapshot/series")
    def get_snapshot_series() -> object:
        snapshot_ts = request.args.get("ts", "")
        if not snapshot_ts:
            return jsonify({"error": "ts parameter required"}), 400
        data = pipeline.get_snapshot_series(snapshot_ts)
        if data is None:
            return jsonify({"error": "No series data for this snapshot"}), 404
        return jsonify(data)

    @bp.route("/forecast/history/color", methods=["POST"])
    def set_snapshot_color() -> object:
        snapshot_ts = request.form.get("snapshot_ts", "")
        snapshot_date = request.form.get("snapshot_date", "")
        label = request.form.get("label", "")
        color = request.form.get("color", "")
        ok, err = pipeline.set_snapshot_color(snapshot_ts, snapshot_date, label, color)
        if not ok:
            return (err or "Color update failed.", 500)
        return ("", 204)

    @bp.route("/forecast/model-data", methods=["GET"])
    def model_data() -> object:
        model_id = request.args.get("model", "monte_carlo")
        exclude_partial = request.args.get("exclude_partial") == "1"

        config = config_svc.load_contract()
        cfg_runs = int(config.get("forecast", {}).get("monte_carlo_runs", 10000))
        try:
            runs = min(int(request.args.get("runs", cfg_runs) or cfg_runs), 20000)
        except (ValueError, TypeError):
            runs = cfg_runs
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        daily_fallback_df = _get_store_df()
        daily_fallback = daily_fallback_df if op_df is None else None

        svc = ForecastingService(config, hist_df, op_df, daily_fallback)
        if exclude_partial:
            _today = _pd.Timestamp("today").normalize()
            if svc.operational_df is not None and not svc.operational_df.empty:
                svc.operational_df = svc.operational_df[
                    svc.operational_df["week_end"] < _today
                ].copy()
            svc._as_of = _today

        data_from_mc = request.args.get("data_from", "").strip() or None
        data_to_mc   = request.args.get("data_to",   "").strip() or None
        if (data_from_mc or data_to_mc) and svc.operational_df is not None and not svc.operational_df.empty:
            win = svc.operational_df.copy()
            if data_from_mc:
                win = win[win["week_start"] >= _pd.to_datetime(data_from_mc)]
            if data_to_mc:
                win = win[win["week_end"] <= _pd.to_datetime(data_to_mc)]
            if not win.empty:
                svc._forecast_op_df = win

        if not svc.has_data():
            return jsonify({"error": "No data available"}), 404

        cs = svc.get_contract_status()
        fc = svc.get_forecast()

        obs_parts = []
        _obs_op = svc._forecast_op_df if svc._forecast_op_df is not None else svc.operational_df
        for df in (_obs_op, svc.historical_df):
            if df is not None and not df.empty and "total_credits_used" in df.columns:
                obs_parts.append(df["total_credits_used"])
        observations = _pd.concat(obs_parts) if obs_parts else _pd.Series(dtype="float64")

        raw_date = cs.get("latest_usage_date")
        try:
            if isinstance(raw_date, _dt.date):
                latest_date = raw_date
            else:
                latest_date = _dt.date.fromisoformat(str(raw_date)[:10])
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid latest_usage_date"}), 500

        ctx = ForecastContext(
            credits_remaining=float(cs["credits_remaining"]),
            weeks_remaining=float(cs["weeks_remaining"]),
            latest_usage_date=latest_date,
            purchased_credits=float(cs["purchased_credits"]),
            forecast_weekly_burn=float(fc["forecast_weekly_burn"]),
            observations=observations,
        )

        try:
            model = get_model(model_id, runs=runs)
        except (ValueError, TypeError):
            return jsonify({"error": f"Unknown model: {model_id!r}"}), 400

        result = model.run(ctx)
        return jsonify(result.to_json_dict())

    return bp
