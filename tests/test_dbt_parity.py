"""dbt port vs transform.py: same raw files in, identical parquet out.

Keeps the two implementations honest while both exist, and pins the dbt
data tests that replace transform.py's raise-on-bad-input behaviour.
Synthetic fixtures only; dbt builds into a temp DuckDB file.
"""
import math
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("dbt.cli.main")

from src import export_marts, transform
from src.gdelt_models import (
    CachedResponse,
    FetchMeta,
    GDELTTimelinePoint,
    GDELTTimelineResponse,
    GDELTTimelineSeries,
)

WEEK1_MON = "20260105"
WEEK2_MON = "20260112"
WEEK3_MON = "20260119"

OUT_NAMES = ["staging_mentions_path", "staging_baseline_path", "weekly_baseline_path", "weekly_mentions_path"]
SORT_KEYS = {
    "staging_mentions_path": ["trend_id", "tier", "date"],
    "staging_baseline_path": ["tier", "date"],
    "weekly_baseline_path": ["tier", "week_start"],
    "weekly_mentions_path": ["trend_id", "tier", "week_start"],
}


def _write_raw(raw_dir: Path, trend_id: str, tier: str, points, fetched_at="2026-01-01T00:00:00+00:00", suffix="0"):
    series = GDELTTimelineSeries(
        series="Article Count", data=[GDELTTimelinePoint(date=d, value=v, norm=n) for d, v, n in points]
    )
    meta = FetchMeta(query="q", timespan="12m", url="https://api.gdeltproject.org/api/v2/doc/doc", fetched_at=fetched_at)
    cached = CachedResponse(meta=meta, response=GDELTTimelineResponse(timeline=[series]))
    path = raw_dir / f"{trend_id}__{tier}__{suffix}.json"
    path.write_text(cached.model_dump_json())
    return path


def _dbt_build(run_dbt, raw_dir: Path) -> tuple:
    # The synthetic models read generator output that isn't in this temp dir.
    return run_dbt({"raw_glob": f"{raw_dir}/*.json"}, exclude="tag:synthetic")


def _outputs(base: Path) -> dict:
    return {n: base / f"{n}.parquet" for n in OUT_NAMES}


@pytest.fixture
def raw_dir(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    return d


def test_dbt_matches_python_transform(tmp_path, raw_dir, run_dbt):
    # Covers: same-week summing, a zero-baseline week (NULL share), a gap week,
    # a baseline week with no trend rows, and two tiers.
    _write_raw(raw_dir, "_baseline", "luxury", [
        (f"{WEEK1_MON}T000000Z", 100.0, 1.0),
        (f"{WEEK2_MON}T000000Z", 0.0, 1.0),
        (f"{WEEK3_MON}T000000Z", 50.0, 1.0),
    ], fetched_at="2026-02-01T12:00:00Z")
    _write_raw(raw_dir, "_baseline", "affordable", [(f"{WEEK1_MON}T000000Z", 40.0, 1.0)])
    # Below MIN_BASELINE_ARTICLES: 3 of 3 articles mentioning a trend is not a 100% share.
    _write_raw(raw_dir, "_baseline", "semi_luxury", [(f"{WEEK1_MON}T000000Z", 3.0, 1.0)])
    _write_raw(raw_dir, "suede", "semi_luxury", [(f"{WEEK1_MON}T000000Z", 3.0, 1.0)])
    _write_raw(raw_dir, "suede", "luxury", [
        (f"{WEEK1_MON}T000000Z", 10.0, 1.0),
        (f"{WEEK1_MON}T120000Z", 5.0, 1.0),
        (f"{WEEK2_MON}T000000Z", 3.0, 1.0),
    ])
    _write_raw(raw_dir, "suede", "affordable", [(f"{WEEK1_MON}T000000Z", 4.0, 1.0)])

    py_dir = tmp_path / "py"
    db_dir = tmp_path / "dbt_out"
    transform.run(raw_dir=raw_dir, **_outputs(py_dir))

    result, warehouse = _dbt_build(run_dbt, raw_dir)
    assert result.success, result.exception
    export_marts.run(warehouse_path=warehouse, **_outputs(db_dir))

    for name in OUT_NAMES:
        py = pd.read_parquet(py_dir / f"{name}.parquet").sort_values(SORT_KEYS[name]).reset_index(drop=True)
        db = pd.read_parquet(db_dir / f"{name}.parquet").sort_values(SORT_KEYS[name]).reset_index(drop=True)
        pd.testing.assert_frame_equal(py, db, obj=name)

    wm = pd.read_parquet(db_dir / "weekly_mentions_path.parquet").set_index(["tier", "week_start"])
    assert math.isnan(wm.loc[("luxury", pd.Timestamp(WEEK2_MON)), "trend_share"])
    assert math.isnan(wm.loc[("semi_luxury", pd.Timestamp(WEEK1_MON)), "trend_share"])


def test_min_baseline_threshold_matches_between_python_and_dbt():
    import yaml
    from src.config import MIN_BASELINE_ARTICLES

    project = yaml.safe_load((Path(__file__).resolve().parent.parent / "dbt" / "dbt_project.yml").read_text())
    assert project["vars"]["min_baseline_articles"] == MIN_BASELINE_ARTICLES


def test_dbt_build_fails_on_orphaned_cache_file(tmp_path, raw_dir, run_dbt):
    # Two baseline files for one tier: what an edited query leaves behind.
    # transform.py would silently sum both; dbt must refuse.
    _write_raw(raw_dir, "_baseline", "luxury", [(f"{WEEK1_MON}T000000Z", 100.0, 1.0)], suffix="old")
    _write_raw(raw_dir, "_baseline", "luxury", [(f"{WEEK1_MON}T000000Z", 100.0, 1.0)], suffix="new")
    _write_raw(raw_dir, "suede", "luxury", [(f"{WEEK1_MON}T000000Z", 10.0, 1.0)])

    result, _ = _dbt_build(run_dbt, raw_dir)
    assert not result.success
    failed = {r.node.name for r in result.result if r.status == "fail"}
    assert "assert_one_raw_file_per_trend_tier" in failed


def test_dbt_build_fails_on_malformed_filename(tmp_path, raw_dir, run_dbt):
    _write_raw(raw_dir, "_baseline", "luxury", [(f"{WEEK1_MON}T000000Z", 100.0, 1.0)])
    (raw_dir / "_baseline__luxury__0.json").rename(raw_dir / "not_a_cache_name.json")

    result, _ = _dbt_build(run_dbt, raw_dir)
    assert not result.success
    failed = {r.node.name for r in result.result if r.status == "fail"}
    assert "assert_raw_filenames_well_formed" in failed
