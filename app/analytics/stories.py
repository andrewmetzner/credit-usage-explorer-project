"""User-summary "stories" — small narrative insights derived from a user's
records. Each builder takes the user's daily records and returns a Story (or
None if it doesn't apply). Add a new insight by writing a builder and listing it
in STORY_BUILDERS; the route/template render whatever comes back.
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field

import pandas as pd

from app.optimization.service import cap_change_date, tier_caps, weeks_in_month


@dataclass
class Story:
    key: str
    title: str
    headline: str                       # main narrative sentence
    detail: str = ""                    # secondary line
    tone: str = "info"                  # info | notable | alert
    icon: str = "•"                     # small glyph shown on the card
    metric: str = ""                    # optional big number/percent
    dates: list = field(default_factory=list)  # [{"date": "...", "note": "..."}]

    def to_dict(self) -> dict:
        return asdict(self)


def _user_daily(user_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize the per-user records to one row context with a parsed date."""
    if user_df is None or user_df.empty:
        return pd.DataFrame()
    cols = {"date_partition", "usage_credits"}
    if not cols.issubset(user_df.columns):
        return pd.DataFrame()
    df = user_df.copy()
    df["_date"] = pd.to_datetime(df["date_partition"], errors="coerce")
    df = df.dropna(subset=["_date"])
    if df.empty:
        return pd.DataFrame()
    df["_credits"] = pd.to_numeric(df["usage_credits"], errors="coerce").fillna(0.0)
    return df


def _month_pace_stats(
    df: pd.DataFrame, year: int, month: int, tier_config: dict, tier_name: str,
    as_of: pd.Timestamp | None = None,
) -> dict | None:
    """Core per-month pace math, shared by the latest-month story and the
    full month-by-month history. `as_of` caps the month at a given date (for
    the current/latest month, which may be in progress); omit for past months."""
    month_df = df[(df["_date"].dt.year == year) & (df["_date"].dt.month == month)]
    if month_df.empty:
        return None

    days_in_month = calendar.monthrange(year, month)[1]
    last_day = as_of if as_of is not None else pd.Timestamp(year, month, days_in_month)

    # Budget for the month = the effective weekly cap for this month's regime,
    # times the weeks in the month. Post-switch (monthly) this equals the monthly
    # cap; pre-switch (weekly) it's the flat weekly cap across the month.
    wim = weeks_in_month(year, month)
    last_week_monday = (last_day - pd.Timedelta(days=int(last_day.weekday()))).normalize()
    caps = tier_caps(tier_config, week_start=last_week_monday)
    weekly_cap = caps.get(tier_name) or caps.get("Baseline") or 0.0
    allowance = float(weekly_cap) * wim
    if allowance <= 0:
        return None

    change = cap_change_date(tier_config)
    weekly_regime = change is not None and last_week_monday < change
    cap_word = "weekly-cap budget" if weekly_regime else "monthly cap"

    daily = month_df.groupby(month_df["_date"].dt.normalize())["_credits"].sum().sort_index()
    spend = float(daily.sum())
    pct = spend / allowance
    days_elapsed = int(last_day.day)
    month_label = f"{calendar.month_name[month]} {year}"

    cumulative = daily.cumsum()
    milestones = []
    for frac in (0.25, 0.50, 0.75, 1.00):
        hit = cumulative[cumulative >= allowance * frac]
        if not hit.empty:
            d = hit.index[0]
            milestones.append({
                "date": str(d.date()),
                "note": f"crossed {frac:.0%} of budget (day {d.day})",
            })

    day_hit_cap = None
    over = cumulative[cumulative >= allowance]
    if not over.empty:
        day_hit_cap = int(over.index[0].day)

    return {
        "year": year, "month": month, "month_label": month_label,
        "spend": spend, "allowance": allowance, "pct": pct,
        "days_in_month": days_in_month, "days_elapsed": days_elapsed,
        "day_hit_cap": day_hit_cap, "milestones": milestones,
        "weekly_regime": weekly_regime, "cap_word": cap_word,
    }


def story_month_pace(
    df: pd.DataFrame, tier_config: dict, tier_name: str, reference_date: object = None,
) -> Story | None:
    """How much of the period budget the user spent in their most recent active
    month, and how quickly they got there. Respects the weekly->monthly switch
    date: months before the switch are measured against the weekly-cap budget.

    The title is deliberately month-agnostic ("Spend Pace") since the story can
    describe either an in-progress current month or a stale past one (a quiet
    user whose last activity predates the workspace's current period) — only
    the body copy below distinguishes the two, via `is_current_month`.
    """
    if df.empty:
        return None
    latest = df["_date"].max()
    stats = _month_pace_stats(df, latest.year, latest.month, tier_config, tier_name, as_of=latest)
    if stats is None:
        return None

    is_current_month = False
    if reference_date is not None and not pd.isna(reference_date):
        ref = pd.Timestamp(reference_date)
        is_current_month = (ref.year, ref.month) == (latest.year, latest.month)

    pct = stats["pct"]
    tone = "alert" if pct >= 1.0 else ("notable" if pct >= 0.80 else "info")
    regime_note = f", {tier_name} tier" + (" · weekly caps" if stats["weekly_regime"] else "")
    title = "Spend Pace"
    headline = (
        f"Spent {pct:.0%} of the {stats['cap_word']} in {stats['month_label']} "
        f"({stats['spend']:,.0f} / {stats['allowance']:,.0f} credits)."
    )
    if stats["day_hit_cap"] is not None:
        detail = (f"Burned through the whole {stats['month_label']} budget by day "
                   f"{stats['day_hit_cap']} of {stats['days_in_month']}.")
    elif is_current_month:
        detail = f"Through day {stats['days_elapsed']} of {stats['days_in_month']}{regime_note}."
    else:
        detail = f"Their most recent active month{regime_note}."

    return Story(
        key="month_pace",
        title=title,
        headline=headline,
        detail=detail,
        tone=tone,
        icon="⏱",
        metric=f"{pct:.0%}",
        dates=stats["milestones"],
    )


def build_month_pace_history(user_df: pd.DataFrame, tier_config: dict, tier_name: str) -> list[dict]:
    """Pace stats for every month present in the user's records (not just the
    latest), oldest first — the "big stories" tab's monthly-pace table."""
    df = _user_daily(user_df)
    if df.empty:
        return []
    latest = df["_date"].max()
    months = sorted({(ts.year, ts.month) for ts in df["_date"]})
    out = []
    for year, month in months:
        is_latest = (year, month) == (latest.year, latest.month)
        stats = _month_pace_stats(
            df, year, month, tier_config, tier_name,
            as_of=latest if is_latest else None,
        )
        if stats is None:
            continue
        pct = stats["pct"]
        out.append({
            "month_label": stats["month_label"],
            "spend": stats["spend"],
            "allowance": stats["allowance"],
            "pct": pct,
            "pct_label": f"{pct:.0%}",
            "cap_word": stats["cap_word"],
            "weekly_regime": stats["weekly_regime"],
            "day_hit_cap": stats["day_hit_cap"],
            "days_in_month": stats["days_in_month"],
            "is_latest": is_latest,
            "tone": "alert" if pct >= 1.0 else ("notable" if pct >= 0.80 else "info"),
        })
    out.reverse()  # most recent month first, for the table
    return out


def story_activity(df: pd.DataFrame, reference_date: object = None) -> Story | None:
    """Recency of activity: how long since the user was last active, measured
    against the most recent date in the whole dataset (not today's calendar)."""
    if df.empty:
        return None
    user_last = df["_date"].max().normalize()
    ref = pd.Timestamp(reference_date).normalize() if reference_date is not None \
        and not pd.isna(reference_date) else user_last
    if ref < user_last:
        ref = user_last
    gap = int((ref - user_last).days)

    recent = df[df["_date"] >= ref - pd.Timedelta(days=13)]
    recent_days = recent.groupby(recent["_date"].dt.normalize())["_credits"].sum().sort_index()
    active_days_14 = int(recent_days.shape[0])

    if gap >= 30:
        tone, icon = "alert", "🔴"
        headline = f"No activity in {gap} days — last active {user_last.date()}."
        detail = f"Latest data is {ref.date()}; this user has likely gone quiet."
    elif gap >= 14:
        tone, icon = "notable", "🟠"
        headline = f"Quiet lately — last active {gap} days ago ({user_last.date()})."
        detail = f"Active on {active_days_14} of the last 14 data-days."
    else:
        tone, icon = "info", "🟢"
        headline = f"Active recently — last used {user_last.date()}."
        detail = f"Active on {active_days_14} of the last 14 data-days."

    dates = [
        {"date": str(d.date()), "note": f"{c:,.0f} cr"}
        for d, c in list(recent_days.items())[-10:]
    ]
    return Story(
        key="activity",
        title="Activity",
        headline=headline,
        detail=detail,
        tone=tone,
        icon=icon,
        metric=(f"{gap}d" if gap else "now"),
        dates=dates,
    )


def _pro_codex_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Boolean masks for 'Pro' usage_type rows and 'Codex'
    usage_type_parsed_type rows, defaulting to all-False when the relevant
    column is missing so callers can combine them without their own
    column-existence check."""
    is_pro = df["usage_type"].astype(str).str.contains("pro", case=False, na=False) \
        if "usage_type" in df.columns else pd.Series(False, index=df.index)
    is_codex = df["usage_type_parsed_type"].astype(str).str.lower().eq("codex") \
        if "usage_type_parsed_type" in df.columns else pd.Series(False, index=df.index)
    return is_pro, is_codex


def story_pro_then_codex(df: pd.DataFrame) -> Story | None:
    """Days where the user used both Pro prompts and Codex — a signal of working
    a hard problem across tools in a single day."""
    if df.empty:
        return None
    raw_type = df.get("usage_type")
    parsed = df.get("usage_type_parsed_type")
    if raw_type is None and parsed is None:
        return None

    is_pro, is_codex = _pro_codex_masks(df)
    if not bool(is_pro.any()) or not bool(is_codex.any()):
        return None

    d = df.copy()
    d["_d"] = d["_date"].dt.normalize()
    d["_is_pro"] = is_pro
    d["_is_codex"] = is_codex
    d["_pro_cr"] = d["_credits"].where(is_pro, 0.0)
    d["_codex_cr"] = d["_credits"].where(is_codex, 0.0)
    by_day = d.groupby("_d").agg(
        pro=("_is_pro", "any"),
        codex=("_is_codex", "any"),
        pro_credits=("_pro_cr", "sum"),
        codex_credits=("_codex_cr", "sum"),
    )
    both = by_day[by_day["pro"] & by_day["codex"]].sort_index()
    if both.empty:
        return None

    dates = [
        {
            "date": str(day.date()),
            "note": f"Pro {row['pro_credits']:,.0f} cr · Codex {row['codex_credits']:,.0f} cr",
        }
        for day, row in both.iterrows()
    ]
    n = len(dates)
    return Story(
        key="pro_then_codex",
        title="Pro + Codex, same day",
        headline=f"Used Pro prompts and Codex on the same day {n} time{'s' if n != 1 else ''}.",
        detail="Often a sign of tackling a hard problem across tools — click to see the dates.",
        tone="notable",
        icon="🧠",
        metric=str(n),
        dates=dates,
    )


STORY_ALERT_METRICS = {
    "inactive": "No activity in N days",
    "burst_cap": "Burned through their cap within N days",
    "pro_codex": "Used Pro + Codex the same day within N days",
}


def _story_alert(
    rid: str, level: str, title: str, detail: str, email: str | None,
    metric: str = "", days: int = 0,
) -> dict:
    if email:
        link_endpoint, link_args = "analytics.user_summary", {"email": email}
    elif metric:
        # Org-wide alert: deep-link to the list of exactly who triggered it.
        link_endpoint, link_args = "analytics.story_matches", {"metric": metric, "days": days}
    else:
        link_endpoint, link_args = "analytics.user_cards_page", {}
    return {
        "id": f"story:{rid}",
        "level": level,
        "title": title,
        "detail": detail,
        "link_endpoint": link_endpoint,
        "link_args": link_args,
    }


def story_metric_matches(
    df: pd.DataFrame,
    metric: str,
    days: int,
    monthly_cap_by_email: dict | None = None,
    default_monthly_cap: float = 400.0,
    reference_date: object = None,
    cap_change_date: object = None,
) -> list[dict]:
    """Per-user match rows for one story metric — who triggered it and why.

    Returns one dict per matching user, with metric-specific fields, so both
    the alert evaluation (counts) and the drill-down page (the actual list)
    share a single implementation of each condition.
    """
    if df is None or df.empty:
        return []
    if not {"date_partition", "usage_credits", "email"}.issubset(df.columns):
        return []
    days = max(int(days or 30), 1)

    d = df.copy()
    d["_date"] = pd.to_datetime(d["date_partition"], errors="coerce")
    d = d.dropna(subset=["_date"])
    if d.empty:
        return []
    d["_credits"] = pd.to_numeric(d["usage_credits"], errors="coerce").fillna(0.0)
    d["_email"] = d["email"].astype(str).str.strip().str.lower()
    ref = pd.Timestamp(reference_date).normalize() if reference_date is not None \
        and not pd.isna(reference_date) else d["_date"].max().normalize()
    caps = monthly_cap_by_email or {}
    names = (
        d.groupby("_email")["name"].last().to_dict()
        if "name" in d.columns else {}
    )

    matches: list[dict] = []
    if metric == "inactive":
        last_by = d.groupby("_email")["_date"].max().dt.normalize()
        gap = (ref - last_by).dt.days
        for em, g in gap[gap >= days].sort_values(ascending=False).items():
            matches.append({
                "email": em,
                "name": names.get(em, ""),
                "last_active": str(last_by[em].date()),
                "days_inactive": int(g),
            })

    elif metric == "burst_cap":
        # Windows that ended before the weekly->monthly switch happened under
        # the weekly-cap regime: the allowance quantity is the same (a month's
        # worth), but calling it "monthly cap" there is anachronistic — flag
        # the regime so alerts and the drill-down can say it honestly.
        #
        # Vectorized as one users x calendar-days matrix: this runs on every
        # page load via the navbar alerts, so it must stay O(matrix), not
        # O(users) pandas calls (the per-user loop cost ~1ms x 1,000 users).
        change = pd.to_datetime(cap_change_date, errors="coerce")
        daily = (
            d.groupby(["_email", d["_date"].dt.normalize()])["_credits"]
            .sum().unstack(fill_value=0.0)
        )
        if not daily.empty:
            full_days = pd.date_range(daily.columns.min(), daily.columns.max(), freq="D")
            daily = daily.reindex(columns=full_days, fill_value=0.0)
            # Rolling N-calendar-day sum ending at each day: cum[t] - cum[t-N].
            cum = daily.cumsum(axis=1)
            rolled = cum - cum.shift(days, axis=1).fillna(0.0)
            peaks = rolled.max(axis=1)
            window_ends = rolled.idxmax(axis=1)
            cap_s = pd.Series(
                [float(caps.get(em, default_monthly_cap)) for em in daily.index],
                index=daily.index,
            )
            hit = (cap_s > 0) & (peaks >= cap_s)
            for em in daily.index[hit]:
                peak = float(peaks[em])
                cap = float(cap_s[em])
                window_end = window_ends[em]
                weekly_era = not pd.isna(change) and window_end < change
                matches.append({
                    "email": em,
                    "name": names.get(em, ""),
                    "peak_spend": round(peak, 2),
                    "monthly_cap": round(cap, 2),
                    "pct_of_cap": round(peak / cap, 4),
                    "window_end": str(window_end.date()),
                    "cap_regime": "weekly" if weekly_era else "monthly",
                })
        matches.sort(key=lambda m: m["pct_of_cap"], reverse=True)

    elif metric == "pro_codex":
        cutoff = ref - pd.Timedelta(days=days - 1)
        w = d[d["_date"] >= cutoff]
        if not w.empty:
            is_pro, is_cx = _pro_codex_masks(w)
            ww = w.assign(_d=w["_date"].dt.normalize(), _pro=is_pro, _cx=is_cx)
            by = ww.groupby(["_email", "_d"]).agg(pro=("_pro", "any"), cx=("_cx", "any"))
            both = by[by["pro"] & by["cx"]].reset_index()
            for em, g in both.groupby("_email"):
                matches.append({
                    "email": em,
                    "name": names.get(em, ""),
                    "times": int(len(g)),
                    "last_date": str(g["_d"].max().date()),
                })
            matches.sort(key=lambda m: m["times"], reverse=True)

    return matches


def evaluate_story_rules(
    df: pd.DataFrame,
    rules: list,
    monthly_cap_by_email: dict | None = None,
    default_monthly_cap: float = 400.0,
    reference_date: object = None,
    cap_change_date: object = None,
) -> list[dict]:
    """Turn story-based alert rules into nav-bell alert dicts.

    Rule shape: {id, name, metric, email (blank=all users), days, enabled}.
    Metrics: 'inactive', 'burst_cap', 'pro_codex'.
    """
    out: list[dict] = []
    if df is None or df.empty:
        return out

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        metric = str(rule.get("metric", "inactive"))
        name = rule.get("name") or "Story alert"
        rid = rule.get("id", "story")
        target = str(rule.get("email", "") or "").strip().lower()
        days = max(int(rule.get("days", 30) or 30), 1)

        matches = story_metric_matches(
            df, metric, days, monthly_cap_by_email, default_monthly_cap, reference_date,
            cap_change_date=cap_change_date,
        )
        if target:
            matches = [m for m in matches if m["email"] == target]
        if not matches:
            continue

        if metric == "inactive":
            if target:
                m = matches[0]
                out.append(_story_alert(rid, "warning", name,
                    f"{target} has no activity in {m['days_inactive']} days "
                    f"(last {m['last_active']}).", target))
            else:
                out.append(_story_alert(rid, "info", name,
                    f"{len(matches):,} user(s) inactive {days}+ days.", None,
                    metric=metric, days=days))

        elif metric == "burst_cap":
            if target:
                m = matches[0]
                cap_word = ("a month's worth of weekly caps"
                            if m.get("cap_regime") == "weekly" else "monthly cap")
                out.append(_story_alert(rid, "warning", name,
                    f"{target} spent {m['peak_spend']:,.0f} cr "
                    f"(>= {m['monthly_cap']:,.0f} = {cap_word}) within {days} days "
                    f"(window ended {m['window_end']}).", target))
            else:
                weekly_era = sum(1 for m in matches if m.get("cap_regime") == "weekly")
                note = (f" ({weekly_era} of them before the monthly-cap switch, under weekly caps)"
                        if weekly_era else "")
                out.append(_story_alert(rid, "warning", name,
                    f"{len(matches):,} user(s) burned a month's worth of cap within {days} days{note}.", None,
                    metric=metric, days=days))

        elif metric == "pro_codex":
            if target:
                m = matches[0]
                out.append(_story_alert(rid, "info", name,
                    f"{target} used Pro + Codex on the same day {m['times']} time(s) in {days} days.", target))
            else:
                out.append(_story_alert(rid, "info", name,
                    f"{len(matches):,} user(s) used Pro + Codex the same day within {days} days.", None,
                    metric=metric, days=days))
    return out


def build_user_stories(
    user_df: pd.DataFrame,
    tier_config: dict,
    tier_name: str,
    reference_date: object = None,
) -> list[dict]:
    """Return the list of applicable stories (as dicts) for a user."""
    df = _user_daily(user_df)
    if df.empty:
        return []
    candidates = [
        story_month_pace(df, tier_config, tier_name, reference_date),
        story_activity(df, reference_date),
        story_pro_then_codex(df),
    ]
    return [s.to_dict() for s in candidates if s]


# ── Org-wide contract timeline ──────────────────────────────────────────────
# The narrative spine of the contract: when it opened, when credits arrived,
# when the governance era changed, how the balance drained, and the forecasts
# taken along the way. Rendered on the Storyboard.

TIMELINE_STEP = 100_000  # balance milestones are reported every 100k burned


def build_contract_timeline(
    df: pd.DataFrame,
    contract: dict,
    tier_config: dict,
    snapshots: list | None = None,
    step: float = TIMELINE_STEP,
    live_forecast: dict | None = None,
) -> list[dict]:
    """Chronological, dated events across the contract.

    Covers: contract start/end, credit-ledger entries (purchased/gifted/
    adjustment), the weekly->monthly cap-era switch, each `step` (100k) the
    remaining balance falls through, the forecast snapshots taken — the
    first and last flagged as the research bookends — and, if supplied, the
    current live forecast's own projected exhaustion.

    Each event: {date, kind, title, detail, tone, icon}.
    """
    from app.shared.credit_ledger import credit_kind_label, normalize_credit_entries

    contract = contract or {}
    events: list[dict] = []

    def add(date, kind, title, detail="", tone="info", icon="•"):
        ts = pd.to_datetime(date, errors="coerce")
        if pd.isna(ts):
            return
        events.append({
            "date": str(ts.date()), "kind": kind, "title": title,
            "detail": detail, "tone": tone, "icon": icon,
        })

    start = pd.to_datetime(contract.get("contract_start_date"), errors="coerce")
    end = pd.to_datetime(contract.get("contract_end_date"), errors="coerce")

    # 1. Contract bookends
    add(start, "contract", "Contract starts", "Credit usage begins counting toward the contract.", "notable", "📄")
    add(end, "contract", "Contract ends", "End of the current credit agreement.", "notable", "🏁")

    # 2. Credit ledger — every purchase / gift / adjustment, on its own date
    entries = normalize_credit_entries(contract)
    for e in entries:
        ed = pd.to_datetime(e.get("date"), errors="coerce")
        if pd.isna(ed) or (not pd.isna(start) and ed < start):
            ed = start
        credits = float(e.get("credits") or 0)
        notes = str(e.get("notes") or "").strip()
        add(ed, "credits", f"+{credits:,.0f} {credit_kind_label(e.get('kind'))}",
            notes or "Added to the credit pool.", "notable", "💳")

    # 3. Governance era change (weekly -> monthly caps)
    change = cap_change_date(tier_config)
    if change is not None:
        add(change, "era", "Caps switch: weekly → monthly",
            "Weeks from here are scored against the monthly cap; earlier weeks "
            "keep the old flat weekly caps.", "notable", "🔀")

    # 4. Balance milestones — each `step` the remaining pool falls through
    daily = _daily_remaining(df, contract, entries, start)
    if not daily.empty:
        peak = float(daily.max())
        level = (int(peak // step)) * step
        seen: set[float] = set()
        for day, remaining in daily.items():
            while level > 0 and remaining <= level:
                if level not in seen:
                    add(day, "balance", f"Credits fall below {level:,.0f}",
                        f"Remaining pool crossed the {level:,.0f} mark.", "info", "📉")
                    seen.add(level)
                level -= step

    # 5. Forecast snapshots — research bookends
    snaps = [s for s in (snapshots or []) if s.get("snapshot_date")]
    snaps.sort(key=lambda s: str(s.get("snapshot_date")))
    if snaps:
        initial = next((s for s in snaps if str(s.get("research_role_initial") or "").lower() == "true"), None)
        final = next((s for s in reversed(snaps) if str(s.get("research_role_final") or "").lower() == "true"), None)
        if initial is None:
            initial = snaps[0]
        if final is None:
            final = snaps[-1]
        add(initial.get("snapshot_date"), "research", "Research: initial forecast",
            _snap_detail(initial), "alert", "🔬")
        if final is not initial:
            add(final.get("snapshot_date"), "research", "Research: end forecast",
                _snap_detail(final), "alert", "🔬")

    # 6. Live forecast — where the current (not a saved snapshot's) forecast
    # projects credits running out, if at all.
    if live_forecast:
        exh_date = live_forecast.get("forecast_exhaustion_date")
        if exh_date:
            exh_week = live_forecast.get("forecast_exhaustion_week")
            detail = f"Week of {exh_week}" if exh_week and str(exh_week) != str(exh_date) else ""
            add(exh_date, "forecast", "Live forecast: projected exhaustion", detail, "alert", "🔥")

    events.sort(key=lambda e: (e["date"], e["kind"]))
    return events


def _snap_detail(snap: dict) -> str:
    """One-line summary of a forecast snapshot for the timeline."""
    bits = []
    label = str(snap.get("label") or "").strip()
    if label:
        bits.append(label)
    for key, fmt in (("credits_remaining", "{:,.0f} remaining"),
                     ("forecast_weekly_burn", "{:,.0f}/wk burn"),
                     ("forecast_exhaustion_date", "exhaustion {}"),
                     ("forecast_exhaustion_week", "week of {}")):
        raw = snap.get(key)
        if raw in (None, "", "nan"):
            continue
        try:
            bits.append(fmt.format(float(raw)) if "{:," in fmt else fmt.format(raw))
        except (TypeError, ValueError):
            bits.append(fmt.format(raw))
    return " · ".join(bits)


def _daily_remaining(df: pd.DataFrame, contract: dict, entries: list, start) -> pd.Series:
    """Daily remaining-credit balance: ledger available on each day minus
    cumulative usage to that day (same ledger-by-date rule as the burndown)."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    if not {"date_partition", "usage_credits"}.issubset(df.columns):
        return pd.Series(dtype="float64")
    d = df[["date_partition", "usage_credits"]].copy()
    d["_day"] = pd.to_datetime(d["date_partition"], errors="coerce").dt.normalize()
    d["_credits"] = pd.to_numeric(d["usage_credits"], errors="coerce").fillna(0.0)
    d = d.dropna(subset=["_day"])
    if not pd.isna(start):
        d = d[d["_day"] >= start.normalize()]
    if d.empty:
        return pd.Series(dtype="float64")

    used = d.groupby("_day")["_credits"].sum().sort_index().cumsum()
    days = pd.date_range(used.index.min(), used.index.max(), freq="D")
    used = used.reindex(days, method="ffill").fillna(0.0)

    available = pd.Series(0.0, index=days)
    for e in entries:
        ed = pd.to_datetime(e.get("date"), errors="coerce")
        if pd.isna(ed) or (not pd.isna(start) and ed < start):
            ed = start
        if pd.isna(ed):
            continue
        available += (days >= ed.normalize()) * float(e.get("credits") or 0)
    return (available - used).clip(lower=0.0)
