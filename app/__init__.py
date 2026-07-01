from __future__ import annotations

from pathlib import Path

from flask import Flask

from .shared.config_service import AppConfig
from .shared.data_store import DataStore
from .shared.ingestion import IngestionPipeline
from .shared.services import Services
from .dashboard.routes import create_dashboard_blueprint
from .analytics.routes import create_analytics_blueprint
from .forecast.routes import create_forecast_blueprint
from .optimization.routes import create_optimization_blueprint
from .settings.routes import create_settings_blueprint


def _fmt_status(value: str | None) -> str:
    if not value:
        return "—"
    return str(value).replace("_", " ").title()


def create_app() -> Flask:
    from config import (
        CONFIG_DIR,
        CURRENT_DATA_PATH,
        CURRENT_DATA_PATH_CACHE,
        DEFAULT_DATA_PATH,
        HISTORICAL_DIR,
        PROCESSED_DIR,
        UPLOADS_DIR,
    )

    _pkg_root = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder=str(_pkg_root.parent / "static"),
    )
    app.secret_key = "bnl-dev-secret"

    # Ensure required folders exist (they may be missing on a fresh install
    # or after the user clears config/data while testing the setup wizard).
    for _d in (CONFIG_DIR, PROCESSED_DIR, HISTORICAL_DIR, UPLOADS_DIR, CURRENT_DATA_PATH.parent):
        _d.mkdir(parents=True, exist_ok=True)

    initial_path = DEFAULT_DATA_PATH
    if CURRENT_DATA_PATH_CACHE.exists():
        try:
            cached = Path(CURRENT_DATA_PATH_CACHE.read_text().strip())
            if cached.exists():
                initial_path = cached
        except Exception:
            pass
    elif CURRENT_DATA_PATH.exists():
        initial_path = CURRENT_DATA_PATH

    store = DataStore(initial_path)
    pipeline = IngestionPipeline(PROCESSED_DIR)
    config_svc = AppConfig(CONFIG_DIR)
    services = Services(store, pipeline, config_svc)

    app.register_blueprint(create_dashboard_blueprint(services))
    app.register_blueprint(create_analytics_blueprint(services))
    app.register_blueprint(create_forecast_blueprint(services))
    app.register_blueprint(create_optimization_blueprint(services))
    app.register_blueprint(create_settings_blueprint(services))
    app.jinja_env.filters["fmt_status"] = _fmt_status

    @app.context_processor
    def inject_nav_alerts() -> dict:
        from .shared.alerts import compute_alerts
        empty = {"nav_alerts": [], "nav_unread_count": 0, "nav_unread_sev": "info"}
        try:
            alerts = compute_alerts(services)
            # Read-state lives server-side; prune resolved conditions so they
            # re-notify if they recur, then flag each alert for the templates.
            read = config_svc.prune_read_alerts(a["id"] for a in alerts)
            for a in alerts:
                a["read"] = a["id"] in read
            unread_levels = [a["level"] for a in alerts if not a["read"]]
            sev = "danger" if unread_levels else "info"
            return {
                "nav_alerts": alerts,
                "nav_unread_count": len(unread_levels),
                "nav_unread_sev": sev,
            }
        except Exception:
            return empty

    # First-run guard: with no contract config yet, steer to the setup wizard
    # (unless the user skipped it). Static + setup/upload endpoints are exempt.
    _SETUP_EXEMPT = {
        "main.setup_page", "main.setup_save_config", "main.setup_skip",
        "main.setup_finish", "main.upload_data",
        "main.diagnostics_page", "main.diagnostics_json",
    }

    @app.before_request
    def _require_setup():
        from flask import redirect, request, session, url_for
        endpoint = request.endpoint or ""
        if endpoint.startswith("static") or endpoint in _SETUP_EXEMPT:
            return None
        if session.get("setup_skipped"):
            return None
        if not config_svc.contract_exists():
            return redirect(url_for("main.setup_page"))
        return None

    return app
