"""Turn the backward-looking analysis into a forward planning tool.

The three steps a brand would take, in order:

  1. Leading indicator. After the runway shows, is a trend's press share
     rising, flat or fading? Momentum = the trend's share of a tier's press
     in the early season (show weeks + POST_SHOW_WEEKS) / its share in the
     PRE_SHOW_WEEKS before. Press momentum, NOT a sales forecast.
  2. Buy plan. Base demand per product from last season's sales, tilted by a
     bounded multiplier from step 1 (see PLAN_MULTIPLIER_* in config.py).
  3. Launch timing. Demand follows press by the delay the lag analysis
     MEASURED for each product (never a hard-coded number): stock and
     marketing land a set number of weeks before demand is expected.

Then two runs of it:

  - backtest: step 1 on the real FW26 press, steps 2-3 for Sage & Blush,
    scored against the simulated season's true demand -- the buy plan with
    and without the trend signal. Reported as measured, including if the
    signal doesn't help.
  - watchlist: step 1 on real press after the SS27 shows (a separate
    pinned-window fetch, data/raw_watchlist/), which is a current
    "what's rising for next spring" list.

Everything about Sage & Blush is SIMULATED and its output files say so in
their names. The watchlist's momentum columns are real GDELT article counts.
"""
import json
import logging
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

import duckdb
import pandas as pd

from src import synthetic_retailer, transform
from src.analyze import elevated_weeks, first_sustained_week, share_series, tier_calendar
from src.config import (
    IN_SEASON_RECENT_WEEKS,
    IN_SEASON_REFERENCE_WEEKS,
    MARKETING_LEAD_WEEKS,
    COMPLETE_WEEK_FRACTION,
    MIN_BASELINE_ARTICLES,
    MIN_MOMENTUM_MENTIONS,
    MOMENTUM_RISING_RATIO,
    PLAN_MULTIPLIER_MAX,
    PLAN_MULTIPLIER_MIN,
    PLANNING_DIR,
    POST_SHOW_WEEKS,
    PRE_SHOW_WEEKS,
    SEASON_SHOWS,
    STOCK_LEAD_WEEKS,
    SYNTHETIC_DIR,
    TIERS,
    WAREHOUSE_PATH,
    WATCHLIST_RAW_DIR,
    WATCHLIST_SEASON,
    WEEKLY_BASELINE_PATH,
    WEEKLY_MENTIONS_PATH,
)
from src.config_models import SyntheticRetailerConfig, TrendsConfig
from src.gdelt_models import GDELTResponseError

log = logging.getLogger("planning")

BACKTEST_SEASON = "FW26"
WEEK = pd.Timedelta(weeks=1)

RISING, FLAT, FADING, THIN, NO_DATA = "rising", "flat", "fading", "too_little_press", "no_data"
INCOMPLETE = "incomplete_data"


# --- step 1: leading indicator ---


def season_windows(season: str) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """(pre_start, show_start, early_end): the pre window is [pre_start,
    show_start), the early season is [show_start, early_end]. Week starts."""
    show_start, show_end = (pd.Timestamp(d) for d in SEASON_SHOWS[season])
    return show_start - PRE_SHOW_WEEKS * WEEK, show_start, show_end + POST_SHOW_WEEKS * WEEK


def window_share(
    weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame, trend_id: str, tier: str,
    start: pd.Timestamp, end: pd.Timestamp,
) -> Tuple[float, float]:
    """(trend mentions, baseline articles) summed over [start, end].

    Pooled, not a mean of weekly shares, so one thin week can't dominate.
    Weeks below MIN_BASELINE_ARTICLES are left out of both sums, the same
    rule that makes their weekly share undefined."""
    b = weekly_baseline_df[
        (weekly_baseline_df["tier"] == tier)
        & weekly_baseline_df["week_start"].between(start, end)
        & (weekly_baseline_df["baseline_mention_count"] >= MIN_BASELINE_ARTICLES)
    ]
    m = weekly_mentions_df[
        (weekly_mentions_df["trend_id"] == trend_id)
        & (weekly_mentions_df["tier"] == tier)
        & weekly_mentions_df["week_start"].isin(b["week_start"])
    ]
    return float(m["mention_count"].sum()), float(b["baseline_mention_count"].sum())


def classify_momentum(ratio: Optional[float], mentions: float) -> str:
    if mentions < MIN_MOMENTUM_MENTIONS:
        return THIN
    if ratio is None or ratio >= MOMENTUM_RISING_RATIO:  # None: zero before, some now
        return RISING
    if ratio <= 1 / MOMENTUM_RISING_RATIO:
        return FADING
    return FLAT


def covers(weekly_baseline_df: pd.DataFrame, tier: str, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    weeks = set(weekly_baseline_df.loc[weekly_baseline_df["tier"] == tier, "week_start"])
    return all(w in weeks for w in pd.date_range(start, end, freq="7D"))


def complete(weekly_baseline_df: pd.DataFrame, tier: str, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    """Every week in [start, end] has at least COMPLETE_WEEK_FRACTION of the
    tier's median weekly baseline. A share is a ratio, so a gap week doesn't
    bias it, but it leaves too few articles to tell a fading trend from a
    missing one -- 0 of 50 looks exactly like "×0.0"."""
    b = weekly_baseline_df[weekly_baseline_df["tier"] == tier].set_index("week_start")["baseline_mention_count"]
    weekly = b.reindex(pd.date_range(start, end, freq="7D")).fillna(0)
    return bool((weekly >= COMPLETE_WEEK_FRACTION * weekly.median()).all())


def breakout_week(
    weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame, trend_id: str, tier: str,
    since: pd.Timestamp, until: pd.Timestamp,
) -> Optional[pd.Timestamp]:
    """First sustained breakout (analyze.py's rule) in [since, until]. The
    caller passes the decision date as `until`: a plan may only use press it
    could have seen."""
    calendar = tier_calendar(weekly_baseline_df, tier)
    calendar = calendar[calendar <= until]
    if calendar.empty:
        return None
    elevated = elevated_weeks(share_series(weekly_mentions_df, trend_id, tier, calendar))
    return first_sustained_week(elevated[elevated.index >= since])


def momentum_between(
    weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame, trends: TrendsConfig,
    ref: Tuple[pd.Timestamp, pd.Timestamp], recent: Tuple[pd.Timestamp, pd.Timestamp],
    fetched: Optional[set] = None,
) -> pd.DataFrame:
    """One row per (trend, tier): pooled press share in `recent` vs `ref`
    (inclusive week-start ranges), plus the first breakout inside `recent`.

    `fetched`: the (trend_id, tier) pairs whose raw file exists. A pair not
    fetched yet would otherwise read as zero mentions -- "too little press"
    -- which looks like a real answer. None means everything was fetched."""
    rows = []
    for tier in TIERS:
        has_data = covers(weekly_baseline_df, tier, ref[0], recent[1])
        is_complete = has_data and complete(weekly_baseline_df, tier, ref[0], recent[1])
        for t in trends.trends:
            pre_m, pre_b = window_share(weekly_mentions_df, weekly_baseline_df, t.id, tier, *ref)
            early_m, early_b = window_share(weekly_mentions_df, weekly_baseline_df, t.id, tier, *recent)
            pre = pre_m / pre_b if pre_b else None
            early = early_m / early_b if early_b else None
            ratio = early / pre if (pre and early is not None) else None
            was_fetched = fetched is None or (t.id, tier) in fetched
            if not (has_data and was_fetched):
                signal = NO_DATA
            elif not is_complete:
                signal = INCOMPLETE
            else:
                signal = classify_momentum(ratio, pre_m + early_m)
            rows.append(
                {
                    "trend_id": t.id,
                    "label": t.label,
                    "tier": tier,
                    "pre_share": pre,
                    "early_share": early,
                    "trend_mentions": int(pre_m + early_m),
                    "momentum": ratio,
                    "signal": signal,
                    "breakout_week": breakout_week(
                        weekly_mentions_df, weekly_baseline_df, t.id, tier, recent[0], recent[1]
                    ) if has_data else None,
                }
            )
    return pd.DataFrame(rows)


def momentum(
    weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame, season: str, trends: TrendsConfig,
    fetched: Optional[set] = None,
) -> pd.DataFrame:
    """Early-season press share (show weeks + POST_SHOW_WEEKS) vs the
    PRE_SHOW_WEEKS before the shows."""
    pre_start, show_start, early_end = season_windows(season)
    out = momentum_between(weekly_mentions_df, weekly_baseline_df, trends,
                           (pre_start, show_start - WEEK), (show_start, early_end), fetched)
    out.insert(0, "season", season)
    return out


def in_season_windows(weekly_baseline_df: pd.DataFrame) -> Tuple[Tuple[pd.Timestamp, pd.Timestamp], ...]:
    """(reference, recent) ending at the latest week every tier present has."""
    last = weekly_baseline_df.groupby("tier")["week_start"].max().min()
    recent_start = last - (IN_SEASON_RECENT_WEEKS - 1) * WEEK
    ref_start = recent_start - IN_SEASON_REFERENCE_WEEKS * WEEK
    return (ref_start, recent_start - WEEK), (recent_start, last)


def in_season(
    weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame, trends: TrendsConfig,
    cfg: SyntheticRetailerConfig, lags: pd.Series, fetched: Optional[set] = None,
) -> pd.DataFrame:
    """Which current-season trends' press is rising right now, in the
    retailer's driver tier, and when demand would follow.

    Demand window = the recent press weeks shifted by the product's measured
    delay. That delay was measured on the SIMULATED store, so the column is
    named for it; a real store would substitute its own."""
    ref, recent = in_season_windows(weekly_baseline_df)
    m = momentum_between(weekly_mentions_df, weekly_baseline_df, trends, ref, recent, fetched)
    m = m[m["tier"] == cfg.driver_tier].copy()
    product = {p.trend_id: p for p in cfg.products if p.trend_id}
    m["product_name"] = m["trend_id"].map(lambda t: product[t].product_name if t in product else None)
    m["delay_weeks_simulated"] = m["trend_id"].map(lambda t: lags.get(product[t].sku_id) if t in product else None)
    start, end = [], []
    for r in m.itertuples():
        lag = r.delay_weeks_simulated
        ok = r.signal == RISING and lag is not None and pd.notna(lag)
        start.append(recent[0] + int(lag) * WEEK if ok else None)
        end.append(recent[1] + int(lag) * WEEK if ok else None)
    m["demand_window_start"], m["demand_window_end"] = start, end
    m.insert(0, "recent_weeks", f"{recent[0].date()}..{recent[1].date()}")
    m.insert(0, "reference_weeks", f"{ref[0].date()}..{ref[1].date()}")
    return m.reset_index(drop=True)


# --- step 2: buy plan ---


def plan_multiplier(signal: str, ratio: Optional[float]) -> float:
    """sqrt(momentum), bounded to [PLAN_MULTIPLIER_MIN, PLAN_MULTIPLIER_MAX].
    Flat, thin or missing press leaves the buy exactly where history puts it."""
    if signal in (FLAT, THIN, NO_DATA, INCOMPLETE):
        return 1.0
    if ratio is None:  # rising from zero
        return PLAN_MULTIPLIER_MAX
    return min(max(math.sqrt(ratio), PLAN_MULTIPLIER_MIN), PLAN_MULTIPLIER_MAX)


def buy_plan(base_weekly: pd.Series, signals: pd.DataFrame, product_trends: pd.Series, season_weeks: int) -> pd.DataFrame:
    """base_weekly: sku -> last season's mean weekly units. signals: one
    tier's momentum rows. product_trends: sku -> trend_id."""
    by_trend = signals.set_index("trend_id")
    out = []
    for sku, base in base_weekly.items():
        sig = by_trend.loc[product_trends[sku]]
        mult = plan_multiplier(sig["signal"], None if pd.isna(sig["momentum"]) else sig["momentum"])
        out.append(
            {
                "sku_id": sku,
                "trend_id": product_trends[sku],
                "signal": sig["signal"],
                "momentum": sig["momentum"],
                "base_weekly_units": base,
                "multiplier": mult,
                "units_without_signal": round(base * season_weeks),
                "units_with_signal": round(base * season_weeks * mult),
            }
        )
    return pd.DataFrame(out)


# --- step 3: launch timing ---


def launch_timing(press_week: Optional[pd.Timestamp], lag_weeks: Optional[int]) -> Dict[str, Optional[pd.Timestamp]]:
    """Demand is expected lag_weeks after the press breakout; stock and
    marketing land STOCK_LEAD_WEEKS / MARKETING_LEAD_WEEKS before that.
    No breakout yet, or no lag the analysis could trust -> no dates."""
    if press_week is None or pd.isna(press_week) or lag_weeks is None or pd.isna(lag_weeks):
        return {"demand_expected_week": None, "stock_in_store_by": None, "marketing_from": None}
    demand = pd.Timestamp(press_week) + int(lag_weeks) * WEEK
    return {
        "demand_expected_week": demand,
        "stock_in_store_by": demand - STOCK_LEAD_WEEKS * WEEK,
        "marketing_from": demand - MARKETING_LEAD_WEEKS * WEEK,
    }


# --- backtest ---


def simulate_season(weekly_demand: pd.Series, units_bought: int) -> dict:
    """One up-front buy, no chase replenishment, so the comparison isolates
    the forecast. A week is sold out if demand exceeded what was left."""
    on_hand, sold_out, lost = units_bought, 0, 0
    for demanded in weekly_demand:
        sold = min(int(demanded), on_hand)
        on_hand -= sold
        if demanded > sold:
            sold_out += 1
            lost += int(demanded) - sold
    return {"weeks_sold_out": sold_out, "units_left_over": on_hand, "units_lost": lost}


def backtest(
    plan: pd.DataFrame, demand_truth: pd.DataFrame, season_start: pd.Timestamp, season_end: pd.Timestamp
) -> pd.DataFrame:
    season = demand_truth[demand_truth["week_start"].between(season_start, season_end)]
    rows = []
    for p in plan.itertuples():
        demand = season[season["sku_id"] == p.sku_id].sort_values("week_start")["units_demanded"]
        without = simulate_season(demand, p.units_without_signal)
        with_ = simulate_season(demand, p.units_with_signal)
        rows.append(
            {
                "sku_id": p.sku_id,
                "season_demand": int(demand.sum()),
                **{f"{k}_without_signal": v for k, v in without.items()},
                **{f"{k}_with_signal": v for k, v in with_.items()},
            }
        )
    return plan.merge(pd.DataFrame(rows), on="sku_id")


def summarize(scored: pd.DataFrame) -> dict:
    def total(col):
        return int(scored[col].sum())

    err_without = (scored["units_without_signal"] - scored["season_demand"]).abs()
    err_with = (scored["units_with_signal"] - scored["season_demand"]).abs()
    return {
        "products": len(scored),
        "products_buy_changed": int((scored["multiplier"] != 1.0).sum()),
        "season_demand": total("season_demand"),
        **{
            f"{k}_{arm}": total(f"{k}_{arm}")
            for k in ("weeks_sold_out", "units_left_over", "units_lost")
            for arm in ("without_signal", "with_signal")
        },
        "buy_error_without_signal": int(err_without.sum()),
        "buy_error_with_signal": int(err_with.sum()),
        "products_closer_with_signal": int((err_with < err_without).sum()),
        "products_further_with_signal": int((err_with > err_without).sum()),
    }


# --- loading ---


def load_weekly(raw_dir: Path) -> Tuple[pd.DataFrame, pd.DataFrame, set]:
    """Weekly marts for a raw cache directory, built in memory with the same
    transform functions (and so the same rules) as the main marts, plus the
    (trend_id, tier) pairs that have a raw file."""
    names = [transform._parse_cache_filename(p) for p in transform._load_raw_files(raw_dir)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        # Same hazard as the main cache: an edited query or window orphans the
        # old file, and both would be summed.
        raise GDELTResponseError(f"{raw_dir} has more than one raw file for {dupes}; delete the stale ones")
    mentions, baseline = transform.split_staging(transform.raw_to_rows(raw_dir))
    wb = transform.weekly_baseline(baseline)
    return transform.weekly_mentions(mentions, wb), wb, set(names)


def measured_lags(con: duckdb.DuckDBPyConnection, driver_tier: str) -> pd.Series:
    """sku -> the lag the analysis recovered, only where it beat chance."""
    df = con.execute(
        "SELECT sku_id, best_lag_searches FROM synthetic.mart_synthetic__recovered_lag "
        "WHERE tier = ? AND clears_noise_ceiling", [driver_tier]
    ).df()
    return df.set_index("sku_id")["best_lag_searches"]


def _plan_for(
    cfg: SyntheticRetailerConfig, signals: pd.DataFrame, base_weekly: pd.Series, lags: pd.Series, season_weeks: int,
    decision_week: pd.Timestamp,
) -> pd.DataFrame:
    trend_skus = {p.sku_id: p.trend_id for p in cfg.products if p.trend_id}
    plan = buy_plan(base_weekly[list(trend_skus)], signals[signals["tier"] == cfg.driver_tier], pd.Series(trend_skus),
                    season_weeks)
    names = {p.sku_id: p.product_name for p in cfg.products}
    plan.insert(1, "product_name", plan["sku_id"].map(names))
    breakout = signals[signals["tier"] == cfg.driver_tier].set_index("trend_id")["breakout_week"]
    plan["measured_lag_weeks"] = plan["sku_id"].map(lags)
    plan["press_breakout_week"] = plan["trend_id"].map(breakout)
    timing = [launch_timing(r.press_breakout_week, r.measured_lag_weeks) for r in plan.itertuples()]
    plan = pd.concat([plan, pd.DataFrame(timing)], axis=1)
    # Press-to-demand delays are weeks, not months: a breakout seen during the
    # early season can already be paying off by the time the buy is placed.
    # Those dates are for marketing and allocating stock already bought.
    expected = pd.to_datetime(plan["demand_expected_week"])
    plan["timing_already_passed"] = (expected < decision_week).where(expected.notna())
    plan.insert(1, "decision_week", decision_week)
    plan.insert(0, "simulated", True)
    return plan


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def run(out_dir: Path = PLANNING_DIR, warehouse_path: Path = WAREHOUSE_PATH) -> dict:
    trends = TrendsConfig.from_yaml()
    cfg = SyntheticRetailerConfig.from_yaml(trends=trends)
    weekly_mentions_df = pd.read_parquet(WEEKLY_MENTIONS_PATH)
    weekly_baseline_df = pd.read_parquet(WEEKLY_BASELINE_PATH)
    sales = pd.read_parquet(SYNTHETIC_DIR / synthetic_retailer.INVENTORY_FILE)
    sales = sales.groupby(["sku_id", "week_start"], as_index=False)["units_sold"].sum()
    demand_truth = pd.read_parquet(SYNTHETIC_DIR / synthetic_retailer.DEMAND_TRUTH_FILE)
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        lags = measured_lags(con, cfg.driver_tier)
    finally:
        con.close()

    # Backtest: decide right after the FW26 early season, using only what was
    # known then -- press up to early_end and sales before the shows -- and
    # sell through to the end of the data.
    fw = momentum(weekly_mentions_df, weekly_baseline_df, BACKTEST_SEASON, trends)
    _, show_start, early_end = season_windows(BACKTEST_SEASON)
    season_start, season_end = early_end + WEEK, demand_truth["week_start"].max()
    season_weeks = len(pd.date_range(season_start, season_end, freq="7D"))
    base = sales[sales["week_start"] < show_start].groupby("sku_id")["units_sold"].mean()
    scored = backtest(_plan_for(cfg, fw, base, lags, season_weeks, season_start), demand_truth, season_start, season_end)
    summary = {
        "simulated": True,
        "retailer": cfg.retailer_name,
        "season": BACKTEST_SEASON,
        "decision_week": str(season_start.date()),
        "season_weeks": season_weeks,
        "base_from": f"{cfg.retailer_name} sales before {show_start.date()}",
        **summarize(scored),
    }

    # Watchlist: real SS27 press, and the plan Sage & Blush would make from it.
    # The SS27 watchlist tracks SS27 trends (trends.yaml, season: SS27). The
    # same recent window also says which FW26 trends are rising in season now.
    wm, wb, fetched = load_weekly(WATCHLIST_RAW_DIR)
    watch = momentum(wm, wb, WATCHLIST_SEASON, TrendsConfig.from_yaml(season=WATCHLIST_SEASON), fetched)
    now = in_season(wm, wb, trends, cfg, lags, fetched) if not wb.empty else pd.DataFrame()

    _write_csv(fw, out_dir / f"momentum_{BACKTEST_SEASON}.csv")
    _write_csv(watch, out_dir / f"watchlist_{WATCHLIST_SEASON}.csv")
    _write_csv(scored, out_dir / f"simulated_backtest_{BACKTEST_SEASON}.csv")
    _write_csv(now, out_dir / "in_season_now.csv")
    tmp = out_dir / "simulated_backtest_summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(out_dir / "simulated_backtest_summary.json")

    log.info("backtest (simulated): %s", summary)
    driver = watch[watch["tier"] == cfg.driver_tier]
    log.info("%s watchlist (%s, real press): %s", WATCHLIST_SEASON, cfg.driver_tier,
             driver.groupby("signal")["trend_id"].apply(list).to_dict())
    if not now.empty:
        log.info("in season now (%s, real press): %s", cfg.driver_tier,
                 now.groupby("signal")["trend_id"].apply(list).to_dict())
    return {"momentum": fw, "watchlist": watch, "backtest": scored, "summary": summary, "in_season": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
