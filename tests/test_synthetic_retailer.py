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


def test_pipeline_recovers_planted_lag(tmp_path, run_dbt):
    planted = 4
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    baseline = 10_000
    _write_weekly_raw(raw_dir, "_baseline", "semi_luxury", pd.Series(baseline, index=CALENDAR))
    for i, trend in enumerate(["suede", "velvet"]):
        counts = (_spiky_driver(CALENDAR, seed=10 + i) * baseline).round()
        _write_weekly_raw(raw_dir, trend, "semi_luxury", counts)

    marts = {name: tmp_path / f"{name}.parquet" for name in
             ["staging_mentions_path", "staging_baseline_path", "weekly_baseline_path", "weekly_mentions_path"]}
    transform.run(raw_dir=raw_dir, **marts)

    cfg_path = tmp_path / "retailer.yaml"
    cfg_path.write_text(yaml.safe_dump(_small_cfg(planted_lag_weeks=planted).model_dump()))
    synthetic_dir = tmp_path / "synthetic"
    synthetic_retailer.run(
        config_path=cfg_path,
        weekly_mentions_path=marts["weekly_mentions_path"],
        weekly_baseline_path=marts["weekly_baseline_path"],
        out_dir=synthetic_dir,
        trends=TRENDS,
    )

    result, warehouse = run_dbt({"raw_glob": f"{raw_dir}/*.json", "synthetic_dir": str(synthetic_dir)})
    assert result.success, result.exception

    con = duckdb.connect(str(warehouse))
    rows = con.execute(
        "SELECT trend_id, best_lag_searches, best_corr_searches, noise_ceiling, clears_noise_ceiling "
        "FROM synthetic.mart_synthetic__recovered_lag WHERE tier = 'semi_luxury' ORDER BY trend_id"
    ).df()
    con.close()

    assert rows["trend_id"].tolist() == ["suede", "velvet"]
    assert rows["best_lag_searches"].tolist() == [planted, planted], rows
    assert rows["clears_noise_ceiling"].all(), rows
