from __future__ import annotations

from hashlib import sha1
from uuid import uuid4
from typing import Any

CREDIT_ENTRY_KINDS: dict[str, str] = {
    "purchased": "Purchased",
    "gifted": "Gifted / grace",
    "adjustment": "Adjustment",
}


def normalize_credit_entries(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return a stable, chronological list of credit ledger entries.

    Older configs may only have a single ``purchased_credits`` total. In that
    case we synthesize one initial entry so the UI still has a log to show.
    """
    contract = contract or {}
    raw_entries = contract.get("credit_entries") or []
    entries: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_entries if isinstance(raw_entries, list) else []):
        if not isinstance(raw, dict):
            continue
        credits = _to_float(raw.get("credits", raw.get("amount", 0)))
        if credits <= 0:
            continue
        date = str(
            raw.get("date")
            or contract.get("purchased_credits_date")
            or contract.get("contract_start_date")
            or ""
        ).strip()
        kind = _normalize_kind(str(raw.get("kind") or raw.get("type") or "purchased"))
        notes = str(raw.get("notes") or "").strip()
        entries.append({
            "id": str(raw.get("id") or _stable_entry_id(idx, date, credits, kind, notes)),
            "date": date,
            "credits": credits,
            "kind": kind,
            "notes": notes,
        })

    if not entries:
        credits = _to_float(contract.get("purchased_credits", 0))
        if credits > 0:
            entries = [{
                "id": "initial",
                "date": str(contract.get("purchased_credits_date") or contract.get("contract_start_date") or "").strip(),
                "credits": credits,
                "kind": "purchased",
                "notes": "Initial allocation",
            }]

    entries.sort(key=lambda e: (e.get("date") or "", e.get("id") or ""))
    return entries


def credit_entries_total(entries: list[dict[str, Any]] | None) -> float:
    return round(sum(_to_float(e.get("credits", 0)) for e in (entries or [])), 2)


def build_credit_entry(
    *,
    date: str,
    credits: float,
    kind: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": uuid4().hex[:8],
        "date": str(date or "").strip(),
        "credits": round(_to_float(credits), 2),
        "kind": _normalize_kind(kind),
        "notes": str(notes or "").strip(),
    }


def sync_credit_ledger(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize the contract ledger and keep the scalar total in sync."""
    contract = contract or {}
    entries = normalize_credit_entries(contract)
    contract["credit_entries"] = entries
    contract["purchased_credits"] = credit_entries_total(entries)
    if entries and not str(contract.get("purchased_credits_date") or "").strip():
        contract["purchased_credits_date"] = str(entries[0].get("date") or "").strip()
    return entries


def credit_kind_label(kind: str) -> str:
    return CREDIT_ENTRY_KINDS.get(_normalize_kind(kind), CREDIT_ENTRY_KINDS["adjustment"])


def _normalize_kind(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k in {"gift", "gifted", "graced", "grace"}:
        return "gifted"
    if k in {"purchased", "purchase", "buy"}:
        return "purchased"
    if k in {"adjustment", "adjust", "correction"}:
        return "adjustment"
    return "adjustment"


def _stable_entry_id(idx: int, date: str, credits: float, kind: str, notes: str) -> str:
    basis = f"{idx}|{date}|{credits:.2f}|{kind}|{notes}"
    return sha1(basis.encode("utf-8")).hexdigest()[:8]


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
