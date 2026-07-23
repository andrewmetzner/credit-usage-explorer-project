"""Multi-contract chained-projection tests: rollover carry-forward, credit
expiration (no rollover), multiple future contracts, and the empty-fallback
case. Run directly:
    .venv\\Scripts\\python.exe testfiles\\test_chained_projection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.forecast.service import build_chained_projection  # noqa: E402


def _contract(cid, start, end, credits, rollover, label=None):
    return {
        "id": cid, "label": label or cid,
        "contract_start_date": start, "contract_end_date": end,
        "rollover_allowed": rollover,
        "credit_entries": [{"id": f"{cid}e", "date": start, "credits": float(credits),
                            "kind": "purchased", "notes": ""}],
        "purchased_credits": float(credits), "purchased_credits_date": start,
    }


def _val_at(points, date):
    """Last value emitted on `date` (post-step / post-expiry)."""
    vals = [p["value"] for p in points if p["date"] == date]
    return vals[-1] if vals else None


def test_expiration_no_rollover() -> None:
    # Contract 1 has a slow burn so a real balance survives to its end, and
    # does NOT roll over -> that balance must expire (drop to 0) at 2026-09-30,
    # then Contract 2 starts fresh at its own start date 2026-10-01.
    contracts = [
        _contract("c1", "2026-04-05", "2026-09-30", 1_000_000, False, "FY26"),
        _contract("c2", "2026-10-01", "2027-03-31", 800_000, False, "FY27"),
    ]
    out = build_chained_projection(contracts, "c1", "2026-08-01", 300_000.0, 1_000.0)
    assert out["expirations"], "expected an expiration event"
    exp = out["expirations"][0]
    assert exp["date"] == "2026-09-30", exp
    assert exp["amount"] > 0, exp
    # The line does NOT cliff to 0 at contract end — it forecasts smoothly, so
    # the point on the end date is the declining leftover (equal to the amount
    # recorded as expired for the overview), not 0.
    end_val = _val_at(out["points"], "2026-09-30")
    assert end_val > 0 and abs(end_val - exp["amount"]) < 1.0, (end_val, exp)
    # At the next contract's start the line resets to that contract's own
    # credits (previous leftover lapsed).
    assert _val_at(out["points"], "2026-10-01") == 800_000.0, out["points"]
    # One injection boundary, at the next contract's START date (not the end).
    assert len(out["boundaries"]) == 1
    assert out["boundaries"][0]["date"] == "2026-10-01"
    assert out["boundaries"][0]["delta"] == 800_000.0
    print("PASS expiration_no_rollover", exp)


def test_rollover_carries_forward() -> None:
    # Same setup but Contract 1 rolls over -> leftover carries, no expiration,
    # and the next-contract-start value = carried balance + new credits.
    contracts = [
        _contract("c1", "2026-04-05", "2026-09-30", 1_000_000, True, "FY26"),
        _contract("c2", "2026-10-01", "2027-03-31", 800_000, False, "FY27"),
    ]
    out = build_chained_projection(contracts, "c1", "2026-08-01", 300_000.0, 1_000.0)
    # C1 rolls over, so nothing expires at ITS end (C2 may still expire at its
    # own later end since it doesn't roll over — that's separate).
    assert "2026-09-30" not in [e["date"] for e in out["expirations"]], out["expirations"]
    carried = _val_at(out["points"], "2026-09-30")
    assert carried > 0, out["points"]
    at_start = _val_at(out["points"], "2026-10-01")
    # carried balance + new credits, minus the ~1 day of burn between the
    # 09-30 carry point and the 10-01 injection date.
    assert 0 <= (carried + 800_000.0) - at_start < 1_000.0, (carried, at_start)
    assert at_start > carried, (carried, at_start)
    print("PASS rollover_carries_forward", {"carried": carried, "after_inject": at_start})


def test_three_contracts_chain() -> None:
    # expire -> rollover -> expire, all reachable with a modest burn.
    contracts = [
        _contract("c1", "2026-04-05", "2026-09-30", 1_000_000, False, "FY26"),
        _contract("c2", "2026-10-01", "2027-03-31", 900_000, True, "FY27"),
        _contract("c3", "2027-04-01", "2027-09-30", 700_000, False, "FY28"),
    ]
    out = build_chained_projection(contracts, "c1", "2026-08-01", 200_000.0, 2_000.0)
    inj_dates = [b["date"] for b in out["boundaries"]]
    assert "2026-10-01" in inj_dates and "2027-04-01" in inj_dates, inj_dates
    exp_dates = [e["date"] for e in out["expirations"]]
    # FY26 expires (no rollover); FY27 rolls over (no expiry at its end).
    assert "2026-09-30" in exp_dates, exp_dates
    assert "2027-03-31" not in exp_dates, exp_dates
    print("PASS three_contracts_chain", {"injections": inj_dates, "expirations": exp_dates})


def test_no_future_contract_returns_empty() -> None:
    contracts = [_contract("c1", "2026-04-05", "2026-09-30", 1_000_000, False)]
    out = build_chained_projection(contracts, "c1", "2026-08-01", 300_000.0, 20_000.0)
    assert out == {"points": [], "boundaries": [], "expirations": []}, out
    print("PASS no_future_contract_returns_empty")


def test_zero_burn_returns_empty() -> None:
    contracts = [
        _contract("c1", "2026-04-05", "2026-09-30", 1_000_000, False),
        _contract("c2", "2026-10-01", "2027-03-31", 800_000, False),
    ]
    out = build_chained_projection(contracts, "c1", "2026-08-01", 300_000.0, 0.0)
    assert out == {"points": [], "boundaries": [], "expirations": []}, out
    print("PASS zero_burn_returns_empty")


if __name__ == "__main__":
    test_expiration_no_rollover()
    test_rollover_carries_forward()
    test_three_contracts_chain()
    test_no_future_contract_returns_empty()
    test_zero_burn_returns_empty()
    print("ALL PASS chained_projection")
