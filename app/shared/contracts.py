"""Multi-contract resolution: which contract governs "now" (or any as-of
date), and rollover carry-forward between adjacent contracts.

Every existing single-contract consumer (ForecastingService, alerts,
diagnostics, the storyboard timeline, ...) still thinks in terms of "the
contract" — one dict with one start/end/ledger/price. `resolve_contract_config`
is the one place that picks which contract from the configured list plays
that role for a given as-of date and returns it in that same legacy shape,
so those call sites don't need to change.
"""
from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

import pandas as pd

from .credit_ledger import build_credit_entry, credit_entries_total, normalize_credit_entries


def _parse(date_str: Any) -> pd.Timestamp:
    return pd.to_datetime(date_str, errors="coerce")


def sort_contracts(contracts: list[dict]) -> list[dict]:
    """Chronological by start date; undated contracts sort last."""
    def key(c: dict):
        ts = _parse(c.get("contract_start_date"))
        return (pd.Timestamp.max if pd.isna(ts) else ts, str(c.get("id") or ""))

    return sorted(contracts or [], key=key)


def contract_ids_for_dates(dates: "pd.Series", contracts: list[dict] | None) -> list[str | None]:
    """Which configured contract's [start, end] window each date falls
    into, or None for a date before the earliest contract's start (or in a
    gap between two contracts). One id per entry, parallel to ``dates`` —
    used by the Summary charts' per-contract scope filter."""
    windows = []
    for c in contracts or []:
        start = _parse((c or {}).get("contract_start_date"))
        if pd.isna(start):
            continue
        end = _parse((c or {}).get("contract_end_date"))
        windows.append((start, end, c.get("id")))
    if not windows:
        return [None] * len(dates)
    out = []
    for d in dates:
        cid = None
        if not pd.isna(d):
            for start, end, wid in windows:
                if d >= start and (pd.isna(end) or d <= end):
                    cid = wid
                    break
        out.append(cid)
    return out


def resolve_active_contract(contracts: list[dict], as_of: Any = None) -> dict | None:
    """The contract that governs `as_of` (default: today).

    Prefers a contract whose [start, end] contains as_of. Otherwise falls
    back to the contract with the latest start <= as_of — this covers both
    "as_of is past every contract's end" (the most recently active one is
    still the meaningful "current" pool, just expired/frozen) and "as_of
    falls in a gap between two defined contracts". If as_of is before every
    contract's start, falls back to the earliest contract.
    """
    dated = [c for c in (contracts or []) if str(c.get("contract_start_date") or "").strip()]
    ordered = sort_contracts(dated)
    if not ordered:
        return None
    cutoff = _parse(as_of) if as_of is not None else pd.Timestamp.today().normalize()
    if pd.isna(cutoff):
        cutoff = pd.Timestamp.today().normalize()

    def starts(c: dict) -> pd.Timestamp:
        return _parse(c.get("contract_start_date"))

    def ends(c: dict) -> pd.Timestamp:
        return _parse(c.get("contract_end_date"))

    containing = [
        c for c in ordered
        if starts(c) <= cutoff and (pd.isna(ends(c)) or cutoff <= ends(c))
    ]
    if containing:
        return containing[-1]

    started = [c for c in ordered if starts(c) <= cutoff]
    if started:
        return started[-1]
    return ordered[0]


def contract_end_balance(pipeline, contract: dict) -> float:
    """Real unused balance in `contract` as of its own end date, using
    actual usage data only (never a projection) — the amount available to
    roll over into the next contract when its `rollover_allowed` is set."""
    from app.forecast.service import ForecastingService

    if pipeline is None:
        return 0.0
    try:
        hist_df = pipeline.get_historical_weekly_summary()
        op_df = pipeline.get_operational_weekly_summary()
    except Exception:
        return 0.0

    wrapped = {
        "contract": contract,
        "pricing": {"current_price_per_credit": float(contract.get("price_per_credit") or 0)},
    }
    svc = ForecastingService(wrapped, hist_df, op_df)
    if not svc.has_data():
        return 0.0
    try:
        return max(float(svc.get_contract_status()["credits_remaining"]), 0.0)
    except Exception:
        return 0.0


def _empty_contract_dict() -> dict:
    return {
        "id": None,
        "label": "",
        "contract_start_date": "",
        "contract_end_date": "",
        "purchased_credits": 0,
        "purchased_credits_date": "",
        "credit_entries": [],
        "rollover_allowed": False,
        "price_per_credit": 0.0,
    }


def resolve_contract_config(config_svc, pipeline=None, as_of: Any = None) -> dict:
    """The legacy-shaped `{"contract", "pricing", "forecast", "contracts"}`
    dict, resolved against whichever contract governs `as_of` (default:
    today). Injects a rollover-in credit entry (in-memory only — never
    persisted here) when the previous contract has rollover enabled and
    `pipeline` is supplied so its real end-of-term balance can be computed.
    """
    doc = config_svc.load_document()
    contracts = doc.get("contracts", [])
    forecast_cfg = doc.get("forecast", {})
    active = resolve_active_contract(contracts, as_of)

    if active is None:
        empty = _empty_contract_dict()
        return {
            "contract": empty,
            "pricing": {"current_price_per_credit": 0.0},
            "forecast": forecast_cfg,
            "contracts": contracts,
        }

    active = copy.deepcopy(active)
    ordered = sort_contracts(contracts)
    idx = next((i for i, c in enumerate(ordered) if c.get("id") == active.get("id")), None)
    if pipeline is not None and idx is not None and idx > 0:
        prior = ordered[idx - 1]
        if bool(prior.get("rollover_allowed")):
            leftover = contract_end_balance(pipeline, prior)
            if leftover > 0:
                entries = normalize_credit_entries(active)
                entries.append(build_credit_entry(
                    date=str(active.get("contract_start_date") or ""),
                    credits=leftover,
                    kind="adjustment",
                    notes=f"Rollover from {prior.get('label') or 'previous contract'}",
                ))
                active["credit_entries"] = entries
                active["purchased_credits"] = credit_entries_total(entries)

    pricing = {"current_price_per_credit": float(active.get("price_per_credit") or 0)}
    return {"contract": active, "pricing": pricing, "forecast": forecast_cfg, "contracts": contracts}
