from __future__ import annotations

from pathlib import Path

from flask import Flask

from .shared.config_service import AppConfig
from .shared.data_store import DataStore
from .shared.ingestion import IngestionPipeline
from .dashboard.routes import create_dashboard_blueprint
from .analytics.routes import create_analytics_blueprint
from .forecast.routes import create_forecast_blueprint
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

    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

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

    app.register_blueprint(create_dashboard_blueprint(store, pipeline, config_svc))
    app.register_blueprint(create_analytics_blueprint(store, pipeline, config_svc))
    app.register_blueprint(create_forecast_blueprint(pipeline, config_svc, store))
    app.register_blueprint(create_settings_blueprint(pipeline, config_svc, store))
    app.jinja_env.filters["fmt_status"] = _fmt_status

    return app
