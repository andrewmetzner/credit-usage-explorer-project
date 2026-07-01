"""
Run from the webapp_v3 directory:
    .venv\\Scripts\\python.exe testfiles\\test_credit_ledger.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.shared.credit_ledger import normalize_credit_entries, sync_credit_ledger  # noqa: E402


def test_synthesized_initial_entry_has_stable_id_and_can_be_removed() -> None:
    contract = {
        "contract_start_date": "2026-04-05",
        "purchased_credits_date": "2026-04-05",
        "purchased_credits": 1_000_000,
    }

    first = normalize_credit_entries(contract)
    second = normalize_credit_entries(contract)
    assert first[0]["id"] == "initial"
    assert second[0]["id"] == first[0]["id"]

    contract["credit_entries"] = [e for e in first if e["id"] != "initial"]
    contract["purchased_credits"] = 0
    sync_credit_ledger(contract)
    assert contract["credit_entries"] == []
    assert contract["purchased_credits"] == 0


if __name__ == "__main__":
    test_synthesized_initial_entry_has_stable_id_and_can_be_removed()
    print("PASS credit_ledger")
