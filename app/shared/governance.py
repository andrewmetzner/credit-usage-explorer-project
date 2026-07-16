"""Per-user governance-tier resolution, shared by every blueprint.

"What tier is this person on?" used to be answered by copy-pasted helpers in
the dashboard, analytics, and optimization blueprints (plus two alert modules
building the same monthly-cap map). This service is the one implementation:
Codex-access groups are resolved out to the user's real credit tier — unless
Codex is the only group they have, in which case it IS their tier.

Obtain one per request via ``services.governance``. Each instance memoizes the
config reads it performs, so repeated calls inside one request don't re-read
the JSON/YAML files, while a fresh instance (next request) sees fresh config.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .config_service import AppConfig


class GovernanceService:
    def __init__(self, config_svc: "AppConfig") -> None:
        self._config_svc = config_svc
        self._cache: dict[str, object] = {}

    def _memo(self, key: str, build):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    # ── Raw config views ─────────────────────────────────────────────────────
    def tier_config(self) -> dict:
        return self._memo("tier_config", self._config_svc.load_tiers)

    def weekly_caps(self, week_start: object = None) -> dict[str, float]:
        """Effective weekly cap per tier (monthly caps divided down)."""
        from app.optimization.service import tier_caps

        if week_start is None:
            return self._memo("weekly_caps", lambda: tier_caps(self.tier_config()))
        return tier_caps(self.tier_config(), week_start=week_start)

    def monthly_caps(self) -> dict[str, float]:
        """Monthly allowance per tier (weekly caps scaled up)."""
        from app.optimization.service import tier_monthly_caps

        return self._memo("monthly_caps", lambda: tier_monthly_caps(self.tier_config()))

    # ── Per-user resolution ──────────────────────────────────────────────────
    def resolved_assignments(self) -> dict[str, str]:
        """email(lowercased) -> governance tier, resolved like the optimization
        engine, with Codex-access users treated as members of the "Codex
        Users" group when it's a configured tier: highest allotment wins, so
        a flagged user rises to Codex Users unless their assigned tier
        already grants more. The Codex badge is independent of all of this."""
        from app.optimization.service import highest_cap_tier, resolve_governance_assignments

        def build() -> dict[str, str]:
            caps = self.weekly_caps()
            resolved = resolve_governance_assignments(
                self._config_svc.load_user_tiers(),
                self._config_svc.load_user_tier_history(),
                caps,
            )
            if "Codex Users" in caps:
                codex = self._memo("codex_access", self._config_svc.load_user_codex_access)
                for email, has_access in codex.items():
                    key = str(email).strip().lower()
                    if not has_access or not key:
                        continue
                    candidates = [t for t in (resolved.get(key, ""), "Codex Users") if t]
                    resolved[key] = highest_cap_tier(candidates, caps)
            return resolved

        return self._memo("resolved", build)

    def tier_for(self, email: object, default: str = "Baseline") -> str:
        return self.resolved_assignments().get(str(email or "").strip().lower(), default)

    def tier_column(self, df: pd.DataFrame) -> pd.Series:
        """Governance tier per row of a usage dataframe (requires an email column)."""
        return (
            df["email"].astype(str).str.strip().str.lower()
            .map(self.resolved_assignments())
            .fillna("Baseline")
        )

    def tier_options(self, df: pd.DataFrame) -> list[str]:
        """Sorted tiers actually in use across the given dataframe's users."""
        if "email" not in getattr(df, "columns", []):
            return []
        return sorted(self.tier_column(df).unique().tolist())

    # ── Tier search (past membership, not just current assignment) ──────────
    def tier_history_map(self) -> dict[str, list[str]]:
        return self._memo("tier_history_map", self._config_svc.load_user_tier_history)

    def tier_search_options(self, df: pd.DataFrame) -> list[str]:
        """Tiers to offer in a tier search/filter dropdown: every tier
        currently assigned to someone in this data, plus any tier that shows
        up in a user's import history. A user who was e.g. a "2026 Summer
        Intern" stays findable under that tier even after highest-allotment
        resolution (resolved_assignments) has since moved their current
        assignment elsewhere."""
        tiers = set(self.tier_options(df))
        for history in self.tier_history_map().values():
            tiers.update(t for t in history if t)
        return sorted(tiers)

    def tier_search_column(self, df: pd.DataFrame, tier_query: str) -> pd.Series:
        """Boolean mask: does each row's user currently hold `tier_query`, or
        did they ever hold it per their import history? Search-only — actual
        cap/assignment logic still uses resolved_assignments()'s
        highest-allotment tier, this just makes past membership searchable."""
        if "email" not in df.columns:
            return pd.Series(False, index=df.index)
        history = self.tier_history_map()
        assignments = self.resolved_assignments()
        emails = df["email"].astype(str).str.strip().str.lower()
        current = emails.map(assignments).fillna("Baseline")
        ever = emails.map(lambda e: tier_query in history.get(e, []))
        return (current == tier_query) | ever

    def monthly_cap_by_email(self, default_tier: str = "Baseline") -> tuple[dict[str, float], float]:
        """(email -> monthly cap, default cap) for story/pace alert evaluation."""
        mcaps = self.monthly_caps()
        default_cap = mcaps.get(default_tier, 400.0)
        cap_by_email = {
            email: mcaps.get(tier, default_cap)
            for email, tier in self.resolved_assignments().items()
        }
        return cap_by_email, default_cap

    def monthly_cap_for(self, tier: object) -> float:
        mcaps = self.monthly_caps()
        return mcaps.get(str(tier or ""), mcaps.get("Baseline", 400.0))

    # ── Codex access (product access badge, not a credit tier) ──────────────
    def has_codex_access(self, email: object) -> bool:
        from app.optimization.service import is_codex_access_tier

        key = str(email or "").strip().lower()
        codex_map = self._memo("codex_access", self._config_svc.load_user_codex_access)
        raw = self._memo("raw_assignments", self._config_svc.load_user_tiers)
        return bool(codex_map.get(key)) or is_codex_access_tier(raw.get(key, ""))
