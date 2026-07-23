from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid4

import yaml

from .alert_rules import DEFAULT_ALERT_RULES, AlertRule
from .credit_ledger import (
    build_credit_entry,
    credit_entries_total,
    normalize_credit_entries,
    sync_credit_ledger,
)


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# Used when no contract_config.yaml exists yet (fresh install / setup wizard).
DEFAULT_CONTRACT_CONFIG: dict = {
    "contracts": [],
    "forecast": {
        "mode": "auto",
        "normalize_weights": True,
        "recent_average_window_weeks": 4,
        "minimum_weeks_for_recent_average": 4,
        "monte_carlo_runs": 10000,
        "snapshot_auto_save": "daily",
        "auto_weight_schedule": [
            {"min_operational_weeks": 0, "max_operational_weeks": 2, "historical_weight": 0.7, "latest_week_weight": 0.3, "recent_average_weight": None},
            {"min_operational_weeks": 3, "max_operational_weeks": 4, "historical_weight": 0.5, "latest_week_weight": 0.2, "recent_average_weight": 0.3},
            {"min_operational_weeks": 5, "max_operational_weeks": 8, "historical_weight": 0.3, "latest_week_weight": 0.2, "recent_average_weight": 0.5},
            {"min_operational_weeks": 9, "max_operational_weeks": None, "historical_weight": 0.2, "latest_week_weight": 0.2, "recent_average_weight": 0.6},
        ],
    },
}


class AppConfig:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.contract_path = config_dir / "contract_config.yaml"
        self.tier_path = config_dir / "tier_policy_config.yaml"
        self.user_tiers_path = config_dir / "user_tier_assignments.json"
        self.user_tier_history_path = config_dir / "user_tier_history.json"
        # email -> True for users whose tierlist groups include a Codex-access
        # group. Codex is product access, not a credit tier, so it's tracked
        # separately from user_tier_history.json rather than living inside it.
        self.user_codex_access_path = config_dir / "user_codex_access.json"
        self.alert_rules_path = config_dir / "alert_rules.json"
        # Analyst-written "stories"/notes pinned to specific users, and
        # story-based alert rules (recency / burst / cross-tool) surfaced in the bell.
        self.user_notes_path = config_dir / "user_notes.json"
        self.story_rules_path = config_dir / "story_alert_rules.json"
        # Dated log of tier changes (email -> [{date, tier, source}]), append-only,
        # so "what tier were they on, as of what date" is answerable — distinct
        # from user_tier_history.json (undated, overwritten per tierlist import).
        self.tier_change_log_path = config_dir / "tier_change_log.json"
        # Which alert conditions the user has dismissed/read (the navbar bell
        # "inbox"). Persisted server-side so read-state survives across browsers
        # and machines, unlike the old per-browser localStorage approach.
        self.read_alerts_path = config_dir / "alert_read_state.json"

    def contract_exists(self) -> bool:
        return self.contract_path.exists()

    def is_contract_configured(self) -> bool:
        """True when at least one configured contract has real dates + credits."""
        try:
            for c in self.load_contracts():
                available = _to_float(c.get("purchased_credits"))
                if available <= 0:
                    available = credit_entries_total(normalize_credit_entries(c))
                if c.get("contract_start_date") and c.get("contract_end_date") and available > 0:
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def _migrate_legacy_document(raw: dict) -> dict:
        """Legacy single-contract shape (`contract:`/`pricing:`) -> the
        multi-contract shape (`contracts:` list). A no-op once a file has
        already been migrated (i.e. already has a `contracts` key)."""
        if "contracts" in raw:
            return raw
        legacy_contract = raw.get("contract")
        forecast_cfg = raw.get("forecast", {})
        if not isinstance(legacy_contract, dict):
            return {"contracts": [], "forecast": forecast_cfg}
        pricing = raw.get("pricing") or {}
        migrated = dict(legacy_contract)
        migrated.setdefault("id", uuid4().hex[:8])
        migrated.setdefault("label", "Contract 1")
        migrated["price_per_credit"] = _to_float(pricing.get("current_price_per_credit"))
        return {"contracts": [migrated], "forecast": forecast_cfg}

    def load_document(self) -> dict:
        """The whole config document in its current (multi-contract) shape.
        Silently migrates and re-persists a legacy on-disk file the first
        time it's loaded post-upgrade."""
        if not self.contract_path.exists():
            return copy.deepcopy(DEFAULT_CONTRACT_CONFIG)
        with open(self.contract_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        doc = self._migrate_legacy_document(raw)
        if "contract" in raw or "pricing" in raw:
            self.save_document(doc)
        return doc

    def save_document(self, doc: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {"contracts": doc.get("contracts", []), "forecast": doc.get("forecast", {})}
        with open(self.contract_path, "w", encoding="utf-8") as f:
            yaml.dump(payload, f, default_flow_style=False, allow_unicode=True)

    def load_contracts(self) -> list[dict]:
        return self.load_document().get("contracts", [])

    def save_contracts(self, contracts: list[dict]) -> None:
        doc = self.load_document()
        doc["contracts"] = contracts
        self.save_document(doc)

    def add_contract(self, fields: dict) -> str:
        contracts = self.load_contracts()
        new_id = uuid4().hex[:8]
        start = str(fields.get("contract_start_date") or "")
        contract = {
            "id": new_id,
            "label": str(fields.get("label") or f"Contract {len(contracts) + 1}"),
            "contract_start_date": start,
            "contract_end_date": str(fields.get("contract_end_date") or ""),
            "price_per_credit": _to_float(fields.get("price_per_credit")),
            "rollover_allowed": bool(fields.get("rollover_allowed")),
            "purchased_credits": 0,
            "purchased_credits_date": str(fields.get("purchased_credits_date") or start),
            "credit_entries": [],
        }
        initial_credits = _to_float(fields.get("purchased_credits"))
        if initial_credits > 0:
            contract["credit_entries"] = [build_credit_entry(
                date=contract["purchased_credits_date"] or start,
                credits=initial_credits,
                kind="purchased",
                notes="Initial allocation",
            )]
        sync_credit_ledger(contract)
        contracts.append(contract)
        self.save_contracts(contracts)
        return new_id

    def update_contract_fields(self, contract_id: str, fields: dict) -> None:
        contracts = self.load_contracts()
        for c in contracts:
            if c.get("id") != contract_id:
                continue
            if "label" in fields:
                c["label"] = str(fields["label"] or c.get("label") or "")
            if "contract_start_date" in fields:
                c["contract_start_date"] = str(fields["contract_start_date"] or "")
            if "contract_end_date" in fields:
                c["contract_end_date"] = str(fields["contract_end_date"] or "")
            if "price_per_credit" in fields:
                c["price_per_credit"] = _to_float(fields["price_per_credit"])
            if "rollover_allowed" in fields:
                c["rollover_allowed"] = bool(fields["rollover_allowed"])
            sync_credit_ledger(c)
            self.save_contracts(contracts)
            return
        raise ValueError(f"No contract with id {contract_id!r}")

    def remove_contract(self, contract_id: str) -> None:
        self.save_contracts([c for c in self.load_contracts() if c.get("id") != contract_id])

    def add_credit_entry(self, contract_id: str, *, date: str, credits: float, kind: str, notes: str = "") -> None:
        contracts = self.load_contracts()
        for c in contracts:
            if c.get("id") != contract_id:
                continue
            entries = normalize_credit_entries(c)
            entries.append(build_credit_entry(date=date, credits=credits, kind=kind, notes=notes))
            c["credit_entries"] = entries
            sync_credit_ledger(c)
            self.save_contracts(contracts)
            return
        raise ValueError(f"No contract with id {contract_id!r}")

    def remove_credit_entry(self, contract_id: str, entry_id: str) -> None:
        contracts = self.load_contracts()
        for c in contracts:
            if c.get("id") != contract_id:
                continue
            entries = normalize_credit_entries(c)
            kept = [e for e in entries if str(e.get("id", "")) != entry_id]
            c["credit_entries"] = kept
            if not kept:
                c["purchased_credits"] = 0
            sync_credit_ledger(c)
            self.save_contracts(contracts)
            return
        raise ValueError(f"No contract with id {contract_id!r}")

    def load_contract(self, as_of=None) -> dict:
        """Legacy-shaped single-contract view, resolved against whichever
        configured contract governs `as_of` (default: today). See
        app/shared/contracts.py for the resolution/rollover rules."""
        from .contracts import resolve_contract_config

        return resolve_contract_config(self, pipeline=None, as_of=as_of)

    def save_contract(self, data: dict) -> None:
        """Compatibility save-back for callers that only ever touch "the"
        (legacy-shaped) contract — writes `data["contract"]` into its slot
        in the full contracts list (matched by id, falling back to today's
        active contract), and `data["forecast"]` as the global forecast
        config."""
        from .contracts import resolve_active_contract

        doc = self.load_document()
        contracts = doc.get("contracts", [])
        edited = dict(data.get("contract") or {})
        pricing = data.get("pricing") or {}
        if "current_price_per_credit" in pricing:
            edited["price_per_credit"] = _to_float(pricing["current_price_per_credit"])

        target_idx = next((i for i, c in enumerate(contracts) if c.get("id") == edited.get("id")), None)
        if target_idx is None:
            active = resolve_active_contract(contracts, None)
            if active is not None:
                target_idx = next((i for i, c in enumerate(contracts) if c.get("id") == active.get("id")), None)
                if target_idx is not None:
                    edited["id"] = contracts[target_idx]["id"]

        sync_credit_ledger(edited)
        if target_idx is not None:
            contracts[target_idx] = edited
        elif edited.get("contract_start_date") or edited.get("credit_entries"):
            edited.setdefault("id", uuid4().hex[:8])
            contracts.append(edited)

        doc["contracts"] = contracts
        if "forecast" in data:
            doc["forecast"] = data["forecast"]
        self.save_document(doc)

    # Tier config is round-tripped with ruamel.yaml so the hand-written
    # annotations (e.g. "# monthly; ~400/week") survive every Settings save.
    # pyyaml remains the fallback if ruamel isn't installed.
    @staticmethod
    def _ruamel():
        try:
            from ruamel.yaml import YAML

            yml = YAML(typ="rt")
            yml.preserve_quotes = True
            yml.width = 4096
            return yml
        except Exception:
            return None

    @staticmethod
    def _merge_into_commented(target, data: dict) -> None:
        """Recursively apply `data` onto a ruamel CommentedMap in place, so
        untouched keys keep their comments. Keys absent from `data` are removed
        (matches plain-dump semantics where the new dict is the whole truth)."""
        for key in [k for k in target.keys() if k not in data]:
            del target[key]
        for key, value in data.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                AppConfig._merge_into_commented(target[key], value)
            else:
                target[key] = value

    def load_tiers(self) -> dict:
        if not self.tier_path.exists():
            return {"tiers": {}}
        yml = self._ruamel()
        with open(self.tier_path, "r", encoding="utf-8") as f:
            if yml is not None:
                return yml.load(f) or {"tiers": {}}
            return yaml.safe_load(f) or {"tiers": {}}

    def save_tiers(self, data: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        yml = self._ruamel()
        if yml is not None:
            try:
                from ruamel.yaml.comments import CommentedMap

                if isinstance(data, CommentedMap):
                    doc = data
                elif self.tier_path.exists():
                    # Plain dict from a caller that built config from scratch:
                    # graft it onto the commented on-disk doc to keep comments.
                    with open(self.tier_path, "r", encoding="utf-8") as f:
                        doc = yml.load(f) or CommentedMap()
                    self._merge_into_commented(doc, data)
                else:
                    doc = data
                with open(self.tier_path, "w", encoding="utf-8") as f:
                    yml.dump(doc, f)
                return
            except Exception:
                pass  # fall through to plain dump rather than lose the save
        with open(self.tier_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def is_tier_editing_locked(self) -> bool:
        """True when per-user tier changes are locked to prevent accidental edits."""
        try:
            return bool(self.load_tiers().get("editing_locked", False))
        except Exception:
            return False

    def set_tier_editing_locked(self, locked: bool) -> None:
        cfg = self.load_tiers()
        cfg["editing_locked"] = bool(locked)
        self.save_tiers(cfg)

    def load_user_tiers(self) -> dict[str, str]:
        if not self.user_tiers_path.exists():
            return {}
        try:
            data = json.loads(self.user_tiers_path.read_text(encoding="utf-8"))
            assignments = data.get("assignments", data) if isinstance(data, dict) else {}
            if not isinstance(assignments, dict):
                return {}
            return {
                str(email).strip().lower(): str(tier).strip()
                for email, tier in assignments.items()
                if str(email).strip() and str(tier).strip()
            }
        except Exception:
            return {}

    def save_user_tiers(self, assignments: dict[str, str]) -> None:
        clean = {
            str(email).strip().lower(): str(tier).strip()
            for email, tier in assignments.items()
            if str(email).strip() and str(tier).strip()
        }
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_tiers_path.write_text(
            json.dumps({"assignments": dict(sorted(clean.items()))}, indent=2),
            encoding="utf-8",
        )

    def load_user_tier_history(self) -> dict[str, list[str]]:
        if not self.user_tier_history_path.exists():
            return {}
        try:
            data = json.loads(self.user_tier_history_path.read_text(encoding="utf-8"))
            histories = data.get("histories", data) if isinstance(data, dict) else {}
            if not isinstance(histories, dict):
                return {}
            clean: dict[str, list[str]] = {}
            for email, tiers in histories.items():
                if not isinstance(tiers, list):
                    continue
                key = str(email).strip().lower()
                values = [str(tier).strip() for tier in tiers if str(tier).strip()]
                if key and values:
                    clean[key] = values
            return clean
        except Exception:
            return {}

    def save_user_tier_history(self, histories: dict[str, list[str]]) -> None:
        clean: dict[str, list[str]] = {}
        for email, tiers in histories.items():
            key = str(email).strip().lower()
            if not key or not isinstance(tiers, list):
                continue
            values = [str(tier).strip() for tier in tiers if str(tier).strip()]
            if values:
                clean[key] = values
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_tier_history_path.write_text(
            json.dumps({"histories": dict(sorted(clean.items()))}, indent=2),
            encoding="utf-8",
        )

    def load_user_codex_access(self) -> dict[str, bool]:
        if not self.user_codex_access_path.exists():
            return {}
        try:
            data = json.loads(self.user_codex_access_path.read_text(encoding="utf-8"))
            access = data.get("codex_access", data) if isinstance(data, dict) else {}
            if not isinstance(access, dict):
                return {}
            return {
                str(email).strip().lower(): bool(flag)
                for email, flag in access.items()
                if str(email).strip() and flag
            }
        except Exception:
            return {}

    def save_user_codex_access(self, codex_access: dict[str, bool]) -> None:
        clean = {
            str(email).strip().lower(): True
            for email, flag in codex_access.items()
            if str(email).strip() and flag
        }
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_codex_access_path.write_text(
            json.dumps({"codex_access": dict(sorted(clean.items()))}, indent=2),
            encoding="utf-8",
        )

    def load_alert_rules(self) -> list[AlertRule]:
        if not self.alert_rules_path.exists():
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]
        try:
            data = json.loads(self.alert_rules_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [AlertRule.from_dict(r) for r in data]
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]
        except Exception:
            return [AlertRule.from_dict(r) for r in DEFAULT_ALERT_RULES]

    def save_alert_rules(self, rules: list) -> None:
        # Normalize whatever we're handed (AlertRule or dict) to clean dicts.
        serializable = [AlertRule.from_dict(r).to_dict() for r in rules]
        self.alert_rules_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    # ── Analyst notes (mutable per-user "stories") ──────────────────────────
    def load_user_notes(self) -> dict[str, list[dict]]:
        """email(lowercased) -> list of note dicts {id, title, text, tone, created}."""
        if not self.user_notes_path.exists():
            return {}
        try:
            data = json.loads(self.user_notes_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_user_notes(self, notes: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_notes_path.write_text(json.dumps(notes, indent=2), encoding="utf-8")

    def add_user_note(self, email: str, note: dict) -> None:
        key = str(email or "").strip().lower()
        if not key:
            return
        notes = self.load_user_notes()
        notes.setdefault(key, []).append(note)
        self.save_user_notes(notes)

    def delete_user_note(self, email: str, note_id: str) -> None:
        key = str(email or "").strip().lower()
        notes = self.load_user_notes()
        if key in notes:
            notes[key] = [n for n in notes[key] if str(n.get("id")) != str(note_id)]
            if not notes[key]:
                notes.pop(key)
            self.save_user_notes(notes)

    # ── Story alert rules (recency / burst / cross-tool, per user) ──────────
    def load_story_alert_rules(self) -> list[dict]:
        if not self.story_rules_path.exists():
            return []
        try:
            data = json.loads(self.story_rules_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_story_alert_rules(self, rules: list) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.story_rules_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")

    # ── Dated tier-change log ────────────────────────────────────────────────
    def load_tier_change_log(self) -> dict[str, list[dict]]:
        if not self.tier_change_log_path.exists():
            return {}
        try:
            data = json.loads(self.tier_change_log_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_tier_change_log(self, log: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.tier_change_log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")

    def record_tier_change(self, email: str, tier: str, source: str, on_date: str | None = None) -> None:
        """Append a dated tier-change entry, skipping no-op repeats (same tier
        as the last recorded entry for this user)."""
        key = str(email or "").strip().lower()
        tier = str(tier or "").strip()
        if not key or not tier:
            return
        from datetime import date as _date
        entry_date = on_date or _date.today().isoformat()
        log = self.load_tier_change_log()
        entries = log.setdefault(key, [])
        if entries and entries[-1].get("tier") == tier:
            return
        entries.append({"date": entry_date, "tier": tier, "source": source})
        self.save_tier_change_log(log)

    def sync_tier_history_to_log(
        self, email: str, history: list[str], source: str, on_date: str | None = None
    ) -> None:
        """Merge an undated tierlist history chain into the dated log.

        A tierlist row only ever tells us the *order* a user moved through
        groups, never when. The chain (oldest first, current tier last) is
        the source of truth for that order, so entries belonging to it are
        always re-emitted in chain order -- even if an unrelated dated event
        (a manual reset, an older single-tier import) previously landed one
        of those same tiers earlier in the log with a real date. That real
        date is kept (real beats "N/A"); only tiers new to the log get
        stamped with `on_date` -- the import date for a fresh upload, or
        "N/A" when backfilling history we have no real date for.
        """
        key = str(email or "").strip().lower()
        clean_history = [str(t).strip() for t in (history or []) if str(t).strip()]
        if not key or not clean_history:
            return
        from datetime import date as _date
        entry_date = on_date if on_date is not None else _date.today().isoformat()
        log = self.load_tier_change_log()
        entries = log.get(key, [])

        chain_set = set(clean_history)
        unrelated = [e for e in entries if e.get("tier") not in chain_set]
        best_for_tier: dict[str, dict] = {}
        for e in entries:
            tier = e.get("tier")
            if tier not in chain_set:
                continue
            current = best_for_tier.get(tier)
            if current is None or (current.get("date") == "N/A" and e.get("date") != "N/A"):
                best_for_tier[tier] = e

        rebuilt_chain = [
            best_for_tier.get(tier) or {"date": entry_date, "tier": tier, "source": source}
            for tier in clean_history
        ]
        new_order = unrelated + rebuilt_chain
        if new_order == entries:
            return
        log[key] = new_order
        self.save_tier_change_log(log)

    # ── Alert read-state (navbar bell "inbox") ──────────────────────────────
    def load_read_alerts(self) -> set[str]:
        """The set of alert ids the user has marked read."""
        if not self.read_alerts_path.exists():
            return set()
        try:
            data = json.loads(self.read_alerts_path.read_text(encoding="utf-8"))
            return {str(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    def save_read_alerts(self, ids) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.read_alerts_path.write_text(
            json.dumps(sorted(str(i) for i in ids), indent=2), encoding="utf-8"
        )

    def prune_read_alerts(self, active_ids) -> set[str]:
        """Drop read ids that no longer match an active alert, so a resolved
        condition that later recurs re-notifies. Returns the surviving set;
        only rewrites the file when something actually changed."""
        read = self.load_read_alerts()
        kept = read & {str(i) for i in active_ids}
        if kept != read:
            self.save_read_alerts(kept)
        return kept

    def mark_read_alerts(self, ids, active_ids) -> set[str]:
        """Add `ids` to the read set (pruned to `active_ids`) and persist."""
        active = {str(i) for i in active_ids}
        read = (self.load_read_alerts() | {str(i) for i in ids}) & active
        self.save_read_alerts(read)
        return read
