"""Synthetic retailer generator + the lag-recovery test.

The recovery test is the point of the whole synthetic layer: plant a lag the
analysis doesn't know, run the real pipeline end to end (raw -> dbt marts ->
generator -> dbt lag marts), and require that exact lag back -- above the
noise ceiling set by the core (no-trend) products. It plants 4 weeks, not
the config's 6, so nothing can pass by having the answer hard-coded.
"""
import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml

from src import synthetic_retailer, transform
from src.config_models import ConfigError, SyntheticRetailerConfig, TrendsConfig
from src.gdelt_models import (
    CachedResponse,
    FetchMeta,
    GDELTTimelinePoint,
    GDELTTimelineResponse,
    GDELTTimelineSeries,
)

TRENDS = TrendsConfig.from_yaml()
REAL_CFG = SyntheticRetailerConfig.from_yaml(trends=TRENDS)


def _cfg(**overrides) -> SyntheticRetailerConfig:
    return SyntheticRetailerConfig.model_validate({**REAL_CFG.model_dump(), **overrides})


def _small_cfg(**overrides) -> SyntheticRetailerConfig:
    """Two trend skus (suede, velvet) + two core skus."""
    keep = {"suede", "velvet", None}
    products = [p.model_dump() for p in REAL_CFG.products if p.trend_id in keep]
    return _cfg(products=products, **overrides)


def _spiky_driver(calendar: pd.DatetimeIndex, seed: int) -> pd.Series:
    # Mostly quiet with occasional bursts: the shape real trend_share has.
    rng = np.random.default_rng(seed)
    base = rng.uniform(0.002, 0.006, len(calendar))
    bursts = rng.random(len(calendar)) < 0.15
    return pd.Series(base + bursts * rng.uniform(0.02, 0.06, len(calendar)), index=calendar)


CALENDAR = pd.date_range("2026-01-05", periods=40, freq="7D")


# --- generator ---


def test_trend_index_lags_and_neutral_fill():
    share = pd.Series([0.0, 2.0, 4.0, np.nan], index=CALENDAR[:4])
    idx = synthetic_retailer.trend_index(share, lag_weeks=1)
    # mean of [0, 2, 4] = 2; shifted one week; leading and undefined weeks -> 1.0
    assert idx.tolist() == [1.0, 0.0, 1.0, 2.0]


def test_trend_index_all_zero_share_is_neutral_not_nan():
    idx = synthetic_retailer.trend_index(pd.Series(0.0, index=CALENDAR[:5]), lag_weeks=2)
    assert (idx == 1.0).all()


def test_generate_is_deterministic():
    cfg = _small_cfg()
    drivers = {"suede": _spiky_driver(CALENDAR, 1), "velvet": _spiky_driver(CALENDAR, 2)}
    a = synthetic_retailer.generate(cfg, drivers, CALENDAR, TRENDS)
    b = synthetic_retailer.generate(cfg, drivers, CALENDAR, TRENDS)
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name], obj=name)


def test_inventory_never_oversells_and_balances():
    cfg = _small_cfg()
    drivers = {"suede": _spiky_driver(CALENDAR, 1), "velvet": _spiky_driver(CALENDAR, 2)}
    inv = synthetic_retailer.generate(cfg, drivers, CALENDAR, TRENDS)[synthetic_retailer.INVENTORY_FILE]
    assert (inv["units_sold"] <= inv["on_hand_start"] + inv["units_received"]).all()
    assert (inv["on_hand_end"] == inv["on_hand_start"] + inv["units_received"] - inv["units_sold"]).all()
    assert (inv["on_hand_end"] >= 0).all()
    assert (inv["units_returned"] <= inv["units_sold"]).all()


# --- config contract ---


def test_product_lag_overrides_retailer_lag():
    products = [p.model_dump() for p in REAL_CFG.products]
    products[0]["planted_lag_weeks"] = 2
    cfg = _cfg(products=products, planted_lag_weeks=6)
    assert [cfg.lag_for(p) for p in cfg.products[:2]] == [2, 6]



def test_config_rejects_unknown_trend_id(tmp_path):
    raw = REAL_CFG.model_dump()
    raw["products"][0]["trend_id"] = "not_a_trend"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="not_a_trend"):
        SyntheticRetailerConfig.from_yaml(path, trends=TRENDS)


def test_config_rejects_size_curve_not_summing_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        _cfg(size_curve={"S": 0.5, "M": 0.4})


def test_config_requires_a_negative_control():
    products = [p.model_dump() for p in REAL_CFG.products if p.trend_id]
    with pytest.raises(ValueError, match="negative control"):
        _cfg(products=products)


# --- end to end: can the pipeline find a lag it wasn't told? ---


def _write_weekly_raw(raw_dir, trend_id, tier, weekly_values):
    points = [
        GDELTTimelinePoint(date=f"{w:%Y%m%d}T000000Z", value=float(v), norm=1.0)
        for w, v in weekly_values.items()
    ]
    meta = FetchMeta(query="q", timespan="12m", url="https://api.gdeltproject.org/api/v2/doc/doc",
                     fetched_at="2026-01-01T00:00:00Z")
    cached = CachedResponse(meta=meta, response=GDELTTimelineResponse(
        timeline=[GDELTTimelineSeries(series="Article Count", data=points)]))
    (raw_dir / f"{trend_id}__{tier}__0.json").write_text(cached.model_dump_json())


def _write_press(raw_dir, press: dict, baseline: int = 10_000):
    """press: {(trend_id, tier): weekly share Series}. One baseline per tier."""
    raw_dir.mkdir()
    for tier in sorted({tier for _, tier in press}):
        calendar = next(s.index for (_, t), s in press.items() if t == tier)
        _write_weekly_raw(raw_dir, "_baseline", tier, pd.Series(baseline, index=calendar))
    for (trend, tier), share in press.items():
        _write_weekly_raw(raw_dir, trend, tier, (share * baseline).round())


def _marts(tmp_path, raw_dir, name):
    paths = {k: tmp_path / f"{name}_{k}.parquet" for k in
             ["staging_mentions_path", "staging_baseline_path", "weekly_baseline_path", "weekly_mentions_path"]}
    transform.run(raw_dir=raw_dir, **paths)
    return paths


def _recover(tmp_path, run_dbt, cfg, press: dict, demand_press: dict = None) -> pd.DataFrame:
    """Run the real pipeline end to end and return the driver tier's recovered
    lags. `press` is what the analysis measures against. `demand_press`, if
    given, is what demand is secretly generated from instead (the null test)."""
    raw_dir = tmp_path / "raw"
    _write_press(raw_dir, press)
    measured = _marts(tmp_path, raw_dir, "measured")
    driving = measured
    if demand_press is not None:
        demand_dir = tmp_path / "raw_demand"
        _write_press(demand_dir, demand_press)
        driving = _marts(tmp_path, demand_dir, "driving")

    cfg_path = tmp_path / "retailer.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg.model_dump()))
    synthetic_dir = tmp_path / "synthetic"
    synthetic_retailer.run(
        config_path=cfg_path,
        weekly_mentions_path=driving["weekly_mentions_path"],
        weekly_baseline_path=driving["weekly_baseline_path"],
        out_dir=synthetic_dir,
        trends=TRENDS,
    )

    result, warehouse = run_dbt({"raw_glob": f"{raw_dir}/*.json", "synthetic_dir": str(synthetic_dir)})
    assert result.success, result.exception
    con = duckdb.connect(str(warehouse))
    rows = con.execute(
        "SELECT trend_id, sku_id, best_lag_searches, best_corr_searches, noise_ceiling, clears_noise_ceiling "
        "FROM synthetic.mart_synthetic__recovered_lag WHERE tier = ? ORDER BY trend_id", [cfg.driver_tier]
    ).df()
    con.close()
    return rows


def _products(trend_ids, **overrides):
    return [
        {**p.model_dump(), **overrides.get(p.trend_id, {})}
        for p in REAL_CFG.products
        if p.trend_id is None or p.trend_id in trend_ids
    ]


YEAR = pd.date_range("2025-09-22", periods=52, freq="7D")


def test_pipeline_recovers_planted_lag(tmp_path, run_dbt):
    planted = 4
    press = {(t, "semi_luxury"): _spiky_driver(CALENDAR, seed=10 + i) for i, t in enumerate(["suede", "velvet"])}
    rows = _recover(tmp_path, run_dbt, _small_cfg(planted_lag_weeks=planted), press)

    assert rows["trend_id"].tolist() == ["suede", "velvet"]
    assert rows["best_lag_searches"].tolist() == [planted, planted], rows
    assert rows["clears_noise_ceiling"].all(), rows


def test_pipeline_recovers_a_different_delay_per_product(tmp_path, run_dbt):
    # One retailer-wide answer could be found by a method that always says the
    # same number. Different delays per product can't be.
    planted = {"suede": 2, "velvet": 9, "oxblood": 5}
    products = _products(planted, **{t: {"planted_lag_weeks": lag} for t, lag in planted.items()})
    press = {(t, "semi_luxury"): _spiky_driver(YEAR, seed=20 + i) for i, t in enumerate(sorted(planted))}
    rows = _recover(tmp_path, run_dbt, _cfg(products=products), press)

    assert dict(zip(rows["trend_id"], rows["best_lag_searches"])) == planted, rows
    assert rows["clears_noise_ceiling"].all(), rows


def test_noisy_demand_mostly_says_cant_tell_rather_than_guessing(tmp_path, run_dbt):
    # A tiny store: ~2 units a week, one search per two units. Poisson noise
    # now rivals the trend effect, so many lags won't clear the noise ceiling
    # -- that's the right answer. The products it IS confident about must
    # have the planted lag. (Not a guarantee: a wrong lag can clear by chance
    # at the false-positive rate the null test measures, ~2% per product.)
    planted = 6
    trend_ids = [t.id for t in TRENDS.trends]
    products = [{**p, "base_weekly_units": 2} for p in _products(trend_ids)]
    cfg = _cfg(products=products, planted_lag_weeks=planted,
               demand={**REAL_CFG.demand.model_dump(), "searches_per_unit": 0.5})
    press = {(t, "semi_luxury"): _spiky_driver(YEAR, seed=30 + i) for i, t in enumerate(trend_ids)}
    rows = _recover(tmp_path, run_dbt, cfg, press)

    confident = rows[rows["clears_noise_ceiling"]]
    print(f"\nnoisy: confident on {len(confident)}/{len(rows)}, "
          f"all at planted lag: {(confident['best_lag_searches'] == planted).all()}")
    assert (confident["best_lag_searches"] == planted).all(), confident
    # Not vacuous: the noise is real (something failed to clear) but not
    # total (something still came through).
    assert 0 < len(confident) < len(rows), rows


def test_null_false_positive_rate_is_low(tmp_path, run_dbt):
    # Demand secretly follows press series the analysis never sees: there is
    # no real relationship to find, so every "found it" is a false positive.
    # Each product's 13 own-trend tries race ~680 decoy tries (13 other trends
    # x 4 tiers x 13 lags), so chance alone wins about 13/690 = 2% of the
    # time. Three products per trend (42) to measure that rate, not one lucky
    # draw; all four tiers, as in the real run.
    trend_ids = [t.id for t in TRENDS.trends]
    tiers = ["luxury", "semi_luxury", "mid_market", "affordable"]
    products = [
        {**p, "sku_id": f"{p['sku_id']}-{copy}"} if p["trend_id"] else p
        for copy in "abc"
        for p in _products(trend_ids)
        if p["trend_id"] or copy == "a"
    ]
    press = {(t, tier): _spiky_driver(YEAR, seed=100 + 10 * i + j)
             for i, t in enumerate(trend_ids) for j, tier in enumerate(tiers)}
    hidden = {(t, tier): _spiky_driver(YEAR, seed=900 + 10 * i + j)
              for i, t in enumerate(trend_ids) for j, tier in enumerate(tiers)}
    rows = _recover(tmp_path, run_dbt, _cfg(products=products), press, demand_press=hidden)

    assert len(rows) == 3 * len(trend_ids)
    false_positives = int(rows["clears_noise_ceiling"].sum())
    print(f"\nnull: {false_positives}/{len(rows)} false positives")
    assert false_positives / len(rows) <= 0.05, rows[rows["clears_noise_ceiling"]]
