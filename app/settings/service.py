from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def force_rmtree(path: Path) -> None:
    """Remove a directory tree, force-unlocking read-only files on Windows."""
    def _on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=_on_error)


def try_snapshot(pipeline, config_svc, label: str) -> None:
    """Save a forecast snapshot if the on_upload trigger is active."""
    from app.forecast.service import ForecastingService
    try:
        cfg = config_svc.load_contract()
        mode = cfg.get("forecast", {}).get("snapshot_auto_save", "daily")
        if mode not in ("on_upload", "both"):
            return
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
        svc = ForecastingService(cfg, hist_df, op_df)
        if svc.has_data():
            svc.save_to_dir(pipeline.processed_dir, once_per_day=False, label=label)
    except Exception:
        pass
