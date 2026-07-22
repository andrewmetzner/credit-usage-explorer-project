from __future__ import annotations

import pandas as pd


def agg_credits(df: pd.DataFrame, group_col: str, top_n: int) -> list[dict]:
    agg_dict: dict = {
        "rows": ("usage_credits", "count"),
        "total_credits": ("usage_credits", "sum"),
    }
    if "email" in df.columns:
        agg_dict["unique_users"] = ("email", "nunique")
    agg = (
        df.groupby(group_col).agg(**agg_dict)
        .reset_index().sort_values("total_credits", ascending=False).head(top_n)
    )
    if "unique_users" not in agg.columns:
        agg["unique_users"] = 0
    return agg.to_dict(orient="records")


def aggregate_by_period(df: pd.DataFrame, col: str, top_n: int) -> list[dict]:
    return agg_credits(df, col, top_n) if col in df.columns else []


def aggregate_by_week(df: pd.DataFrame, top_n: int) -> list[dict]:
    if "date_partition" not in df.columns:
        return []
    wdf = df.copy()
    wdf["date_partition"] = pd.to_datetime(wdf["date_partition"], errors="coerce")
    wdf = wdf.dropna(subset=["date_partition"])
    if wdf.empty:
        return []
    wdf["week"] = wdf["date_partition"].dt.to_period("W").dt.start_time.dt.strftime("%Y-%m-%d")
    return agg_credits(wdf, "week", top_n)


def aggregate_by_period_fmt(
    df: pd.DataFrame, period: str, fmt: str, col_name: str, top_n: int
) -> list[dict]:
    if "date_partition" not in df.columns:
        return []
    pdf = df.copy()
    pdf["date_partition"] = pd.to_datetime(pdf["date_partition"], errors="coerce")
    pdf = pdf.dropna(subset=["date_partition"])
    if pdf.empty:
        return []
    pdf[col_name] = pdf["date_partition"].dt.to_period(period).dt.start_time.dt.strftime(fmt)
    return agg_credits(pdf, col_name, top_n)


class Leaderboards:
    """Builds every Leaderboard-page ranking from one (already-filtered) usage frame.

    Keeps the aggregation logic in one cohesive place instead of inline in the
    route, so each board is named, reusable, and easy to test.
    """

    def __init__(self, df: pd.DataFrame, top_n: int = 25) -> None:
        self.df = df
        self.top_n = top_n

    def _with_token_message_quantities(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        if "usage_units" in frame.columns and "usage_quantity" in frame.columns:
            frame["tokens_qty"] = frame["usage_quantity"].where(frame["usage_units"] == "tokens", 0.0)
            frame["messages_qty"] = frame["usage_quantity"].where(frame["usage_units"] == "counts", 0.0)
        else:
            frame["tokens_qty"] = 0.0
            frame["messages_qty"] = 0.0
        return frame

    def by_user(self) -> list[dict]:
        cols = [c for c in ["name", "email"] if c in self.df.columns]
        if not cols:
            return []
        agg = (
            self._with_token_message_quantities(self.df).groupby(cols)
            .agg(
                rows=("usage_credits", "count"),
                total_credits=("usage_credits", "sum"),
                total_tokens=("tokens_qty", "sum"),
                total_messages=("messages_qty", "sum"),
            )
            .reset_index().sort_values("total_credits", ascending=False).head(self.top_n)
        )
        return agg.to_dict(orient="records")

    def by_user_type(self) -> list[dict]:
        cols = [c for c in ["name", "email", "usage_type_parsed_type"] if c in self.df.columns]
        if len(cols) < 2:
            return []
        agg = (
            self.df.groupby(cols)
            .agg(rows=("usage_credits", "count"), total_credits=("usage_credits", "sum"))
            .reset_index().sort_values("total_credits", ascending=False).head(self.top_n)
        )
        return agg.to_dict(orient="records")

    def by_dimension(self, col: str, limit: int | None = None) -> list[dict]:
        """Credits-per-dimension board (models, usage types). ``limit=None`` keeps all rows."""
        if col not in self.df.columns:
            return []
        agg_dict: dict = {"rows": ("usage_credits", "count"), "total_credits": ("usage_credits", "sum")}
        if "email" in self.df.columns:
            agg_dict["unique_users"] = ("email", "nunique")
        agg = (
            self.df.groupby(col).agg(**agg_dict)
            .reset_index().sort_values("total_credits", ascending=False)
        )
        if limit:
            agg = agg.head(limit)
        if "unique_users" not in agg.columns:
            agg["unique_users"] = 0
        return agg.to_dict(orient="records")

    def by_model(self) -> list[dict]:
        return self.by_dimension("usage_type_model", self.top_n)

    def by_usage_type(self) -> list[dict]:
        return self.by_dimension("usage_type_parsed_type")

    def by_tier(self) -> list[dict]:
        return self.by_dimension("tier")

    def biggest_single(self) -> list[dict]:
        if "usage_credits" not in self.df.columns:
            return []
        cols = [
            c for c in (
                "name", "email", "usage_credits", "usage_type_parsed_type",
                "usage_type_model", "usage_type_io", "usage_quantity",
                "usage_units", "date_partition", "usage_type",
            )
            if c in self.df.columns
        ]
        return (
            self.df[cols].sort_values("usage_credits", ascending=False)
            .head(self.top_n).to_dict(orient="records")
        )

    def daily(self) -> list[dict]:
        return aggregate_by_period(self.df, "date_partition", self.top_n)

    def weekly(self) -> list[dict]:
        return aggregate_by_week(self.df, self.top_n)

    def monthly(self) -> list[dict]:
        return aggregate_by_period_fmt(self.df, "M", "%Y-%m", "month", self.top_n)

    def yearly(self) -> list[dict]:
        return aggregate_by_period_fmt(self.df, "Y", "%Y", "year", self.top_n)
