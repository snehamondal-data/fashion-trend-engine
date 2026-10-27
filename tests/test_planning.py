"""Planning: momentum, buy plan, launch timing, season simulation.

Synthetic fixtures only. The backtest's real numbers are reported, not
asserted -- whether the trend signal helps is a finding, not a contract."""
import json
import math

import pandas as pd
import pytest

from src import planning
from src.config import (
    MIN_BASELINE_ARTICLES,
    MIN_MOMENTUM_MENTIONS,
    MOMENTUM_RISING_RATIO,
    PLAN_MULTIPLIER_MAX,
    PLAN_MULTIPLIER_MIN,
)
from src.config_models import TrendsConfig
from src.gdelt_models import GDELTResponseError

TRENDS = TrendsConfig.from_yaml()


def _weekly(weeks, baseline, mentions, trend="suede", tier="semi_luxury"):
    """weekly_mentions / weekly_baseline frames shaped like the marts."""
    wb = pd.DataFrame({"tier": tier, "week_start": weeks, "baseline_mention_count": baseline})
    wm = pd.DataFrame({"trend_id": trend, "tier": tier, "week_start": weeks, "mention_count": mentions})
    wm = wm[wm["mention_count"] > 0]  # gap weeks are absent from the mart, as in transform.py
    wm = wm.merge(wb, on=["tier", "week_start"])
    wm["trend_share"] = wm["mention_count"] / wm["baseline_mention_count"]
    return wm, wb


def test_season_windows_bracket_the_shows():
    pre_start, show_start, early_end = planning.season_windows("SS27")
    assert show_start == pd.Timestamp("2026-09-07")
    assert pre_start == pd.Timestamp("2026-07-13")  # 8 weeks before
    assert early_end == pd.Timestamp("2026-10-19")  # last show week + 2


def test_window_share_pools_counts_and_skips_thin_weeks():
    weeks = pd.date_range("2026-01-05", periods=3, freq="7D")
    # The middle week has a 2-article baseline: 2 of 2 would be a "100%" week.
    wm, wb = _weekly(weeks, [100, MIN_BASELINE_ARTICLES - 3, 300], [10, 2, 20])
    mentions, baseline = planning.window_share(wm, wb, "suede", "semi_luxury", weeks[0], weeks[-1])
    assert (mentions, baseline) == (30, 400)  # pooled: 30/400, not mean(10%, 6.7%)


@pytest.mark.parametrize(
    "ratio, mentions, expected",
    [
        (MOMENTUM_RISING_RATIO, 50, "rising"),
        (MOMENTUM_RISING_RATIO - 0.01, 50, "flat"),
        (1 / MOMENTUM_RISING_RATIO, 50, "fading"),
        (None, 50, "rising"),  # nothing before the shows, something after
        (3.0, MIN_MOMENTUM_MENTIONS - 1, "too_little_press"),
    ],
)
def test_classify_momentum(ratio, mentions, expected):
    assert planning.classify_momentum(ratio, mentions) == expected


def test_momentum_end_to_end_and_no_data_outside_coverage():
    pre_start, show_start, early_end = planning.season_windows("SS27")
    weeks = pd.date_range(pre_start, early_end, freq="7D")
    before = weeks < show_start
    # 1% of press before the shows, 3% after: momentum 3x.
    wm, wb = _weekly(weeks, 1000, [10 if b else 30 for b in before])
    out = planning.momentum(wm, wb, "SS27", TRENDS).set_index(["trend_id", "tier"])
    row = out.loc[("suede", "semi_luxury")]
    assert row["momentum"] == pytest.approx(3.0)
    assert row["signal"] == "rising"
    # velvet: covered tier, never mentioned -> too little press, not "fading".
    assert out.loc[("velvet", "semi_luxury"), "signal"] == "too_little_press"
    # luxury has no baseline weeks at all in this fixture.
    assert out.loc[("suede", "luxury"), "signal"] == "no_data"


def test_breakout_week_cannot_see_past_the_decision_date():
    weeks = pd.date_range("2026-01-05", periods=20, freq="7D")
    counts = [10] * 14 + [80] * 6  # breakout in week 14
    wm, wb = _weekly(weeks, 1000, counts)
    assert planning.breakout_week(wm, wb, "suede", "semi_luxury", weeks[0], weeks[-1]) == weeks[14]
    assert planning.breakout_week(wm, wb, "suede", "semi_luxury", weeks[0], weeks[13]) is None


@pytest.mark.parametrize(
    "signal, ratio, expected",
    [
        ("rising", 4.0, PLAN_MULTIPLIER_MAX),  # sqrt 2.0, capped
        ("rising", 1.69, 1.3),
        ("fading", 0.25, PLAN_MULTIPLIER_MIN),  # sqrt 0.5, floored
        ("rising", None, PLAN_MULTIPLIER_MAX),  # from zero
        ("flat", 1.4, 1.0),
        ("too_little_press", 9.0, 1.0),
        ("no_data", None, 1.0),
    ],
)
def test_plan_multiplier_is_bounded(signal, ratio, expected):
    assert planning.plan_multiplier(signal, ratio) == pytest.approx(expected)


def test_buy_plan_applies_multiplier_to_base():
    signals = pd.DataFrame({"trend_id": ["suede", "velvet"], "signal": ["rising", "flat"], "momentum": [1.69, 1.2]})
    plan = planning.buy_plan(pd.Series({"A": 10.0, "B": 10.0}), signals, pd.Series({"A": "suede", "B": "velvet"}), 20)
    assert plan.set_index("sku_id")["units_without_signal"].tolist() == [200, 200]
    assert plan.set_index("sku_id")["units_with_signal"].tolist() == [260, 200]


def test_launch_timing_counts_back_from_expected_demand():
    t = planning.launch_timing(pd.Timestamp("2026-09-21"), 6)
    assert t["demand_expected_week"] == pd.Timestamp("2026-11-02")
    assert t["stock_in_store_by"] == pd.Timestamp("2026-10-26")
    assert t["marketing_from"] == pd.Timestamp("2026-10-19")


@pytest.mark.parametrize("press_week, lag", [(None, 6), (pd.Timestamp("2026-09-21"), None), (pd.NaT, 6)])
def test_launch_timing_without_breakout_or_trusted_lag_gives_no_dates(press_week, lag):
    assert all(v is None for v in planning.launch_timing(press_week, lag).values())


def test_simulate_season_counts_stockouts_lost_and_leftover():
    demand = pd.Series([4, 4, 4])
    assert planning.simulate_season(demand, 10) == {"weeks_sold_out": 1, "units_left_over": 0, "units_lost": 2}
    assert planning.simulate_season(demand, 20) == {"weeks_sold_out": 0, "units_left_over": 8, "units_lost": 0}


def test_backtest_scores_both_arms_against_true_demand():
    weeks = pd.date_range("2026-04-20", periods=4, freq="7D")
    truth = pd.DataFrame({"week_start": weeks, "sku_id": "A", "units_demanded": [5, 5, 5, 5]})
    plan = pd.DataFrame({"sku_id": ["A"], "multiplier": [1.5], "units_without_signal": [12], "units_with_signal": [18]})
    scored = planning.backtest(plan, truth, weeks[0], weeks[-1])
    s = planning.summarize(scored)
    assert s["season_demand"] == 20
    assert (s["units_lost_without_signal"], s["units_lost_with_signal"]) == (8, 2)
    assert (s["buy_error_without_signal"], s["buy_error_with_signal"]) == (8, 2)
    assert s["products_closer_with_signal"] == 1


def test_load_weekly_refuses_orphaned_watchlist_files(tmp_path):
    for digest in ("aaaa", "bbbb"):
        (tmp_path / f"suede__luxury__{digest}.json").write_text(json.dumps({}))
    with pytest.raises(GDELTResponseError, match="more than one raw file"):
        planning.load_weekly(tmp_path)


# --- seasons and the in-season view ---


def test_trends_default_to_fw26_so_the_main_pipeline_is_unchanged():
    fw = TrendsConfig.from_yaml()
    ss = TrendsConfig.from_yaml(season="SS27")
    assert {t.season for t in fw.trends} == {"FW26"} and len(fw.trends) == 14
    assert ss.trends and {t.season for t in ss.trends} == {"SS27"}
    assert len(TrendsConfig.from_yaml(season=None).trends) == len(fw.trends) + len(ss.trends)


def test_every_ss27_trend_is_sourced():
    for t in TrendsConfig.from_yaml(season="SS27").trends:
        assert t.evidence.brands, t.id
        assert str(t.evidence.source).startswith("https://"), t.id


def test_watchlist_ingest_fetches_both_seasons_main_ingest_only_fw26():
    from src import ingest_gdelt
    from src.config_models import BrandsConfig

    brands = BrandsConfig.from_yaml()
    main = {t for t, _, _ in ingest_gdelt.build_tasks(brands, TrendsConfig.from_yaml())}
    watch = {t for t, _, _ in ingest_gdelt.build_tasks(brands, TrendsConfig.from_yaml(season=None))}
    assert not any(t.startswith("ss27_") for t in main)
    assert {"ss27_satin", "suede"} <= watch


def test_in_season_windows_end_at_latest_week_all_tiers_share():
    weeks = pd.date_range("2026-07-13", periods=15, freq="7D")
    _, wb_a = _weekly(weeks, 1000, [1] * 15, tier="semi_luxury")
    _, wb_b = _weekly(weeks[:-1], 1000, [1] * 14, tier="luxury")  # one week behind
    ref, recent = planning.in_season_windows(pd.concat([wb_a, wb_b]))
    assert recent == (weeks[-5], weeks[-2])  # 4 weeks ending where both tiers have data
    assert ref == (weeks[-13], weeks[-6])  # the 8 before


def test_in_season_rising_trend_gets_a_demand_window_from_the_measured_delay():
    from src.config_models import SyntheticRetailerConfig

    cfg = SyntheticRetailerConfig.from_yaml(trends=TRENDS)
    weeks = pd.date_range("2026-07-13", periods=15, freq="7D")
    wm, wb = _weekly(weeks, 1000, [10] * 11 + [40] * 4)  # suede x4 in the last 4 weeks
    suede_sku = next(p.sku_id for p in cfg.products if p.trend_id == "suede")
    out = planning.in_season(wm, wb, TRENDS, cfg, pd.Series({suede_sku: 6})).set_index("trend_id")

    row = out.loc["suede"]
    assert row["signal"] == "rising"
    assert row["delay_weeks_simulated"] == 6
    assert row["demand_window_start"] == weeks[11] + pd.Timedelta(weeks=6)
    assert row["demand_window_end"] == weeks[14] + pd.Timedelta(weeks=6)
    # No measured delay for velvet (not in the lags) -> no window, even if it rose.
    assert pd.isna(out.loc["velvet", "demand_window_start"])


def test_unfetched_pair_is_no_data_not_too_little_press():
    pre_start, _, early_end = planning.season_windows("SS27")
    weeks = pd.date_range(pre_start, early_end, freq="7D")
    wm, wb = _weekly(weeks, 1000, [0] * len(weeks))
    out = planning.momentum(wm, wb, "SS27", TRENDS, fetched={("suede", "semi_luxury")}).set_index(["trend_id", "tier"])
    assert out.loc[("suede", "semi_luxury"), "signal"] == "too_little_press"  # fetched, genuinely quiet
    assert out.loc[("velvet", "semi_luxury"), "signal"] == "no_data"  # never fetched


def test_gap_week_in_either_window_is_incomplete_not_fading():
    # The live bug: GDELT dropped whole days, a tier's coverage fell from ~220
    # articles a week to 0-11, and a trend with 0 mentions there read as
    # "fading x0.0". A collection gap must never become a momentum figure.
    pre_start, show_start, early_end = planning.season_windows("SS27")
    weeks = pd.date_range(pre_start, early_end, freq="7D")
    baseline = [220] * len(weeks)
    baseline[-3] = 0  # a lost week inside the early season
    counts = [5 if w < show_start else 0 for w in weeks]
    wm, wb = _weekly(weeks, baseline, counts)
    out = planning.momentum(wm, wb, "SS27", TRENDS).set_index(["trend_id", "tier"])
    assert out.loc[("suede", "semi_luxury"), "signal"] == "incomplete_data"
    assert planning.plan_multiplier("incomplete_data", 0.0) == 1.0

    # Same data with the week intact: genuinely fading.
    wm, wb = _weekly(weeks, [220] * len(weeks), counts)
    out = planning.momentum(wm, wb, "SS27", TRENDS).set_index(["trend_id", "tier"])
    assert out.loc[("suede", "semi_luxury"), "signal"] == "fading"


def test_a_quieter_week_that_is_not_a_gap_stays_complete():
    weeks = pd.date_range("2026-07-13", periods=8, freq="7D")
    _, wb = _weekly(weeks, [200, 210, 120, 220, 190, 200, 205, 215], [1] * 8)  # 120 >= half of ~200
    assert planning.complete(wb, "semi_luxury", weeks[0], weeks[-1])
    _, wb = _weekly(weeks, [200, 210, 11, 220, 190, 200, 205, 215], [1] * 8)
    assert not planning.complete(wb, "semi_luxury", weeks[0], weeks[-1])
