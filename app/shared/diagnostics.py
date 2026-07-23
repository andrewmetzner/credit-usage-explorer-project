"""Built-in diagnostics / health checks.

An object-oriented, extensible self-test framework. Each check subclasses
``Check`` and returns a ``DiagnosticResult``; ``Diagnostics`` runs them all and
rolls up an overall status. To add a check: subclass ``Check`` and append it to
``Diagnostics.CHECKS``.

Surfaced at /debug (HTML) and /debug.json (machine-readable).
"""
from __future__ import annotations

import platform
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# Status ranking for rolling up an overall result.
_RANK = {"ok": 0, "warn": 1, "error": 2}


@dataclass
class DiagnosticResult:
    name: str
    status: str = "ok"                       # ok | warn | error
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
            "elapsed_ms": self.elapsed_ms,
        }


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "not installed"


class Check(ABC):
    """Base class for a single diagnostic check."""
    name: str = "Check"

    @abstractmethod
    def run(self, ctx: "Diagnostics") -> DiagnosticResult:
        ...


class EnvironmentCheck(Check):
    name = "Environment"

    def run(self, ctx):
        details = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "flask": _pkg_version("flask"),
            "pandas": _pkg_version("pandas"),
            "numpy": _pkg_version("numpy"),
            "scikit-learn": _pkg_version("scikit-learn"),
            "openpyxl": _pkg_version("openpyxl"),
        }
        missing = [k for k in ("scikit-learn", "openpyxl") if details[k] == "not installed"]
        status = "warn" if missing else "ok"
        summary = ("Missing: " + ", ".join(missing)) if missing else "All core packages present."
        return DiagnosticResult(self.name, status, summary, details)


class ConfigCheck(Check):
    name = "Configuration"

    def run(self, ctx):
        cfg = ctx.config_svc
        exists = cfg.contract_exists()
        configured = cfg.is_contract_configured()
        contracts = []
        active = {}
        try:
            contracts = cfg.load_contracts()
            active = cfg.load_contract().get("contract", {})
        except Exception:
            pass
        details = {
            "config_dir": str(getattr(cfg, "config_dir", "")),
            "contract_file_exists": exists,
            "contracts_configured": len(contracts),
            "active_contract": active.get("label") or "—",
            "active_contract_start": active.get("contract_start_date") or "—",
            "active_contract_end": active.get("contract_end_date") or "—",
            "purchased_credits": active.get("purchased_credits") or 0,
            "alert_rules": len(cfg.load_alert_rules()),
        }
        if not exists:
            return DiagnosticResult(self.name, "error", "No contract config — run the setup wizard.", details)
        if not configured:
            return DiagnosticResult(self.name, "warn", "Contract config present but incomplete.", details)
        return DiagnosticResult(self.name, "ok", "Contract is configured.", details)


class DataCheck(Check):
    name = "Data"

    def run(self, ctx):
        try:
            df = ctx.store.data.df
        except Exception as exc:
            return DiagnosticResult(self.name, "error", f"Could not read data: {exc}")
        rows = len(df)
        required = ["date_partition", "usage_credits", "email", "usage_type"]
        missing = [c for c in required if c not in df.columns]
        details: dict[str, Any] = {
            "source": str(getattr(ctx.store, "path", "")),
            "rows": rows,
            "columns": len(df.columns),
            "missing_required": missing or "none",
            "memory_mb": round(df.memory_usage(deep=True).sum() / 1e6, 2) if rows else 0,
        }
        if rows and "date_partition" in df.columns:
            d = pd.to_datetime(df["date_partition"], errors="coerce")
            if d.notna().any():
                details["date_range"] = f"{d.min().date()} → {d.max().date()}"
                details["unparseable_dates"] = int(d.isna().sum())
        if rows == 0:
            return DiagnosticResult(self.name, "warn", "No data loaded — upload a sheet.", details)
        if missing:
            return DiagnosticResult(self.name, "warn", f"Missing required columns: {', '.join(missing)}", details)
        return DiagnosticResult(self.name, "ok", f"{rows:,} records loaded.", details)


class PipelineCheck(Check):
    name = "Pipeline & Snapshots"

    def run(self, ctx):
        p = ctx.pipeline
        try:
            status = p.status()
        except Exception:
            status = {}
        snapshots = 0
        try:
            snapshots = len(p.get_forecast_history())
        except Exception:
            pass
        details = {
            "historical_weeks": status.get("historical_weeks", 0),
            "operational_weeks": status.get("operational_weeks", 0),
            "snapshots": snapshots,
            "processed_dir": str(getattr(p, "processed_dir", "")),
        }
        return DiagnosticResult(self.name, "ok", f"{snapshots} snapshot(s) saved.", details)


class ForecastCheck(Check):
    name = "Forecast Engine"

    def run(self, ctx):
        try:
            svc = ctx.services.build_forecasting_service()
            if not svc.has_data():
                return DiagnosticResult(self.name, "warn", "No data to forecast yet.")
            cs = svc.get_contract_status()
            fc = svc.get_forecast()
            details = {
                "pacing_status": cs.get("pacing_status"),
                "forecast_status": fc.get("forecast_status"),
                "credits_remaining": round(float(cs.get("credits_remaining", 0))),
                "weekly_burn": round(float(fc.get("forecast_weekly_burn", 0))),
                "weeks_remaining": round(float(cs.get("weeks_remaining", 0)), 1),
            }
            return DiagnosticResult(self.name, "ok", "Forecast computes cleanly.", details)
        except Exception as exc:
            return DiagnosticResult(self.name, "error", f"Forecast failed: {exc}")


class AlertsCheck(Check):
    name = "Alerts"

    def run(self, ctx):
        try:
            from .alerts import compute_alerts
            alerts = compute_alerts(ctx.services)
        except Exception as exc:
            return DiagnosticResult(self.name, "error", f"Alert evaluation failed: {exc}")
        by_level = {}
        for a in alerts:
            by_level[a["level"]] = by_level.get(a["level"], 0) + 1
        details = {
            "active_alerts": len(alerts),
            "by_level": by_level or "none",
            "titles": [a["title"] for a in alerts] or "none",
        }
        return DiagnosticResult(self.name, "ok", f"{len(alerts)} active alert(s).", details)


class Diagnostics:
    """Runs every registered check and rolls up an overall status."""

    CHECKS: list[type[Check]] = [
        EnvironmentCheck, ConfigCheck, DataCheck,
        PipelineCheck, ForecastCheck, AlertsCheck,
    ]

    def __init__(self, services):
        self.services = services
        self.store = services.store
        self.pipeline = services.pipeline
        self.config_svc = services.config_svc

    def run_all(self) -> dict[str, Any]:
        results: list[DiagnosticResult] = []
        for cls in self.CHECKS:
            check = cls()
            start = time.perf_counter()
            try:
                res = check.run(self)
            except Exception as exc:  # a check must never break the page
                res = DiagnosticResult(check.name, "error", f"Check crashed: {exc}")
            res.elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            results.append(res)

        overall = "ok"
        for r in results:
            if _RANK[r.status] > _RANK[overall]:
                overall = r.status
        return {
            "overall": overall,
            "results": results,
            "total_ms": round(sum(r.elapsed_ms for r in results), 1),
        }
