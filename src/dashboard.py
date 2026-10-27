"""Marts + warehouse -> docs/index.html, a single self-contained dashboard page.

Static on purpose: it's served by GitHub Pages straight from docs/, so anyone
with the link can read it without running Python. The data is embedded as
JSON -- weekly article-count shares (no article content) and Sage & Blush
numbers, which are SIMULATED and labelled so on the page.

Every number on the page is computed here from the marts; the template holds
no hard-coded findings, so a rebuild after new data can't leave stale claims.
Trend shares come from analyze.share_series on analyze.tier_calendar, the same
dense-calendar rule the signal detection uses (gap week = 0, zero-baseline
week = undefined).
"""
import json
import logging
import math
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from src.analyze import share_series, tier_calendar
from src.config import (
    PLANNING_DIR,
    ROOT,
    SEASON_SHOWS,
    TIERS,
    TREND_DIFFUSION_CSV,
    WAREHOUSE_PATH,
    WATCHLIST_SEASON,
    WEEKLY_BASELINE_PATH,
    WEEKLY_MENTIONS_PATH,
)
from src.config_models import SyntheticRetailerConfig, TrendsConfig

log = logging.getLogger("dashboard")

TEMPLATE_PATH = Path(__file__).with_name("dashboard_template.html")
DASHBOARD_PATH = ROOT / "docs" / "index.html"

TIER_LABELS = {
    "luxury": "Luxury",
    "semi_luxury": "Semi-luxury",
    "mid_market": "Mid-market shoes & bags",
    "affordable": "Affordable",
}
# Fixed categorical slots: color follows the tier, never its position.
TIER_SLOTS = {"luxury": 1, "semi_luxury": 2, "affordable": 3, "mid_market": 4}

FW26 = SEASON_SHOWS["FW26"]

# "Always on": a trend whose typical week in one tier is at least ALWAYS_ON_SHARE
# of that tier's coverage and ALWAYS_ON_RATIO times its typical week anywhere
# else. Breakout detection can't see these -- they never break out (suede).
ALWAYS_ON_SHARE = 5.0  # percent
ALWAYS_ON_RATIO = 3.0


def _clean(v):
    """JSON-safe scalar: NaN/NaT -> None, numpy -> python."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def _week(v):
    return None if pd.isna(v) else pd.Timestamp(v).strftime("%Y-%m-%d")


def _index(values: pd.Series) -> list:
    """Values / their mean * 100, so series of different units share one axis."""
    mean = values.mean(skipna=True)
    if not mean or math.isnan(mean):
        return [None] * len(values)
    return [_clean(round(v / mean * 100, 1)) for v in values]


def always_on(share: dict, tiers: list) -> list:
    """Trends that sit high in one tier every week rather than spiking."""
    out = []
    for trend_id, by_tier in share.items():
        medians = {t: pd.Series(by_tier[t], dtype=float).median(skipna=True) for t in tiers}
        for tier, med in medians.items():
            others = [v for t, v in medians.items() if t != tier and pd.notna(v)]
            if pd.notna(med) and med >= ALWAYS_ON_SHARE and others and med >= ALWAYS_ON_RATIO * max(max(others), 1e-9):
                out.append({"id": trend_id, "tier": tier, "median": round(float(med), 1), "others": round(float(max(others)), 1)})
    return out


def planning_payload(planning_dir: Path) -> dict:
    """Outputs of src/planning.py. Watchlist momentum is real press; the
    backtest and plan are for the simulated retailer."""
    watch = pd.read_csv(planning_dir / f"watchlist_{WATCHLIST_SEASON}.csv")
    backtest = pd.read_csv(planning_dir / "simulated_backtest_FW26.csv")
    now_path = planning_dir / "in_season_now.csv"
    # Empty until the watchlist window has been fetched.
    now = pd.read_csv(now_path) if now_path.stat().st_size > 1 else pd.DataFrame()
    summary = json.loads((planning_dir / "simulated_backtest_summary.json").read_text())

    def rows(df, cols):
        return [{c: _clean(getattr(r, c)) for c in cols} for r in df.itertuples()]

    return {
        "season": WATCHLIST_SEASON,
        "shows": list(SEASON_SHOWS[WATCHLIST_SEASON]),
        "watchlist": rows(watch, ["trend_id", "tier", "signal", "momentum", "trend_mentions", "breakout_week"]),
        "backtest": rows(backtest, ["product_name", "trend_id", "signal", "multiplier", "units_without_signal",
                                    "units_with_signal", "season_demand", "weeks_sold_out_without_signal",
                                    "weeks_sold_out_with_signal"]),
        "backtest_summary": summary,
        "ss27_trends": [{"id": t.id, "label": t.label, "brands": t.evidence.brands, "source": str(t.evidence.source)}
                        for t in TrendsConfig.from_yaml(season=WATCHLIST_SEASON).trends],
        "in_season": rows(now, ["trend_id", "label", "product_name", "signal", "momentum", "trend_mentions",
                                "delay_weeks_simulated", "demand_window_start", "demand_window_end"]) if len(now) else [],
        "in_season_weeks": {"reference": now["reference_weeks"].iloc[0], "recent": now["recent_weeks"].iloc[0]}
        if len(now) else None,
    }


def build_payload(
    weekly_mentions: pd.DataFrame,
    weekly_baseline: pd.DataFrame,
    diffusion: pd.DataFrame,
    con: duckdb.DuckDBPyConnection,
    retailer: SyntheticRetailerConfig,
    planning: Optional[dict] = None,
) -> dict:
    trends = TrendsConfig.from_yaml().trends
    tiers = [t for t in TIERS if not tier_calendar(weekly_baseline, t).empty]

    calendars = {t: tier_calendar(weekly_baseline, t) for t in tiers}
    weeks = sorted(set().union(*[set(c) for c in calendars.values()]))
    week_index = pd.DatetimeIndex(weeks)

    share = {}
    for trend in trends:
        share[trend.id] = {}
        for tier in tiers:
            s = share_series(weekly_mentions, trend.id, tier, calendars[tier]).reindex(week_index)
            share[trend.id][tier] = [_clean(round(v * 100, 3)) if pd.notna(v) else None for v in s]

    driver = retailer.driver_tier
    sku_week = con.execute(
        "SELECT trend_id, week_start, searches, units_sold, core_size_sold_out "
        "FROM synthetic.fct_synthetic__sku_week WHERE trend_id IS NOT NULL ORDER BY week_start"
    ).df()
    products = con.execute(
        "SELECT trend_id, sku_id, product_name FROM synthetic.stg_synthetic__products WHERE trend_id IS NOT NULL"
    ).df().set_index("trend_id")
    lagcorr = con.execute(
        "SELECT candidate_trend_id AS trend_id, lag_weeks, corr_searches FROM synthetic.mart_synthetic__lag_correlation "
        "WHERE is_own_trend AND tier = ? ORDER BY lag_weeks", [driver]
    ).df()
    recovered = con.execute(
        "SELECT trend_id, best_lag_searches, best_corr_searches, best_lag_units_sold, noise_ceiling, "
        "clears_noise_ceiling FROM synthetic.mart_synthetic__recovered_lag WHERE tier = ?", [driver]
    ).df().set_index("trend_id")

    retail = {}
    for trend in trends:
        if trend.id not in products.index:
            continue
        sw = sku_week[sku_week["trend_id"] == trend.id].set_index("week_start").reindex(week_index)
        press = share_series(weekly_mentions, trend.id, driver, calendars[driver]).reindex(week_index)
        rec = recovered.loc[trend.id]
        retail[trend.id] = {
            "product": products.loc[trend.id, "product_name"],
            "press": _index(press),
            "searches": _index(sw["searches"]),
            "units": _index(sw["units_sold"]),
            "sold_out_weeks": int(sw["core_size_sold_out"].astype("boolean").fillna(False).sum()),
            "lagcorr": [
                {"lag": int(r.lag_weeks), "r": _clean(round(r.corr_searches, 3)) if pd.notna(r.corr_searches) else None}
                for r in lagcorr[lagcorr["trend_id"] == trend.id].itertuples()
            ],
            "best_lag": _clean(rec["best_lag_searches"]),
            "best_r": _clean(round(rec["best_corr_searches"], 2)),
            "best_lag_units": _clean(rec["best_lag_units_sold"]),
            "clears": bool(rec["clears_noise_ceiling"]),
            "ceiling": _clean(round(rec["noise_ceiling"], 2)),
            "planted": retailer.lag_for(next(p for p in retailer.products if p.trend_id == trend.id)),
        }

    lag_cols = {
        "lux_semi": "lag_luxury_to_semi_weeks",
        "semi_aff": "lag_semi_to_affordable_weeks",
        "semi_mid": "lag_semi_to_mid_market_weeks",
    }
    rows = []
    labels = {t.id: t.label for t in trends}
    for r in diffusion.itertuples():
        rows.append(
            {
                "id": r.trend_id,
                "label": labels[r.trend_id],
                "classification": r.classification,
                **{k: _clean(getattr(r, c, None)) for k, c in lag_cols.items()},
            }
        )

    def _count(col, sign):
        s = diffusion[col].dropna() if col in diffusion else pd.Series(dtype=float)
        return {"n": int(len(s)), "hits": int((s < 0).sum() if sign < 0 else (s > 0).sum())}

    return {
        "weeks": [w.strftime("%Y-%m-%d") for w in week_index],
        "fw26": list(FW26),
        "tiers": [{"id": t, "label": TIER_LABELS[t], "slot": TIER_SLOTS[t]} for t in tiers],
        "trends": rows,
        "share": share,
        "retail": retail,
        "retailer": retailer.retailer_name,
        "driver_tier": TIER_LABELS[driver],
        "planted_lag": retailer.planted_lag_weeks,
        "always_on": always_on(share, tiers),
        "planning": planning,
        "stats": {
            "trends": len(rows),
            "semi_before_lux": _count("lag_luxury_to_semi_weeks", -1),
            "aff_after_semi": _count("lag_semi_to_affordable_weeks", +1),
            "recovered": int(sum(1 for v in retail.values() if v["best_lag"] == v["planted"] and v["clears"])),
            "retail_trends": len(retail),
        },
    }


def render(payload: dict, template: str) -> str:
    # "</" inside a <script> block would end it early; JSON allows "<\/".
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    if template.count("/*__DATA__*/") != 1:
        raise ValueError(f"{TEMPLATE_PATH} must contain exactly one /*__DATA__*/ placeholder")
    return template.replace("/*__DATA__*/", data)


def run(
    out_path: Path = DASHBOARD_PATH, warehouse_path: Path = WAREHOUSE_PATH, planning_dir: Path = PLANNING_DIR
) -> Path:
    try:
        planning = planning_payload(planning_dir)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Planning outputs missing from {planning_dir}; run `make planning` first: {exc}") from exc
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        payload = build_payload(
            pd.read_parquet(WEEKLY_MENTIONS_PATH),
            pd.read_parquet(WEEKLY_BASELINE_PATH),
            pd.read_csv(TREND_DIFFUSION_CSV),
            con,
            SyntheticRetailerConfig.from_yaml(),
            planning,
        )
    except duckdb.CatalogException as exc:
        raise RuntimeError(f"Synthetic marts missing from {warehouse_path}; run `make synthetic` first: {exc}") from exc
    finally:
        con.close()

    html = render(payload, TEMPLATE_PATH.read_text())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".html.tmp")
    tmp.write_text(html)
    tmp.replace(out_path)
    log.info("dashboard written: %s (%d KB)", out_path, len(html) // 1024)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
