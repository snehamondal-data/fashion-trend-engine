"""DuckDB warehouse (built by dbt) -> the parquet files the rest of the
pipeline reads.

analyze.py, charts.py and the agent all read data/staging + data/marts
parquet, so those paths stay the contract and nothing downstream changes.
Writes are temp-file-then-rename, same as transform.py: dbt-duckdb's
"external" materialization would write parquet directly, but a crash
mid-COPY can leave a truncated file at the real path.
"""
import logging
from pathlib import Path

import duckdb
import pandas as pd

from src.config import (
    STAGING_BASELINE_PATH,
    STAGING_MENTIONS_PATH,
    WAREHOUSE_PATH,
    WEEKLY_BASELINE_PATH,
    WEEKLY_MENTIONS_PATH,
)
from src.transform import _atomic_write_parquet

log = logging.getLogger("export_marts")

# Explicit column lists: a column added to a dbt model doesn't leak into the
# parquet contract until it's added here on purpose.
EXPORTS = {
    "stg_gdelt__mentions": ["trend_id", "tier", "date", "value", "norm", "ingested_at"],
    "stg_gdelt__baseline": ["tier", "date", "value", "norm", "ingested_at"],
    "fct_weekly_baseline": ["tier", "week_start", "baseline_mention_count"],
    "fct_weekly_mentions": [
        "trend_id", "tier", "week_start", "mention_count", "baseline_mention_count", "trend_share",
    ],
}

SORT_KEYS = {
    "stg_gdelt__mentions": None,
    "stg_gdelt__baseline": None,
    "fct_weekly_baseline": ["tier", "week_start"],
    "fct_weekly_mentions": ["trend_id", "tier", "week_start"],
}


def run(
    warehouse_path: Path = WAREHOUSE_PATH,
    staging_mentions_path: Path = STAGING_MENTIONS_PATH,
    staging_baseline_path: Path = STAGING_BASELINE_PATH,
    weekly_baseline_path: Path = WEEKLY_BASELINE_PATH,
    weekly_mentions_path: Path = WEEKLY_MENTIONS_PATH,
) -> None:
    targets = {
        "stg_gdelt__mentions": staging_mentions_path,
        "stg_gdelt__baseline": staging_baseline_path,
        "fct_weekly_baseline": weekly_baseline_path,
        "fct_weekly_mentions": weekly_mentions_path,
    }
    # Not read_only: when dbt ran in-process (tests) its connection to this
    # file is still open, and DuckDB refuses a second one with a different config.
    con = duckdb.connect(str(warehouse_path))
    try:
        # Without this, TIMESTAMPTZ columns come back in the machine's local
        # zone and ingested_at would differ between laptops.
        con.execute("SET TimeZone = 'UTC'")
        for relation, columns in EXPORTS.items():
            order = SORT_KEYS[relation]
            sql = f"SELECT {', '.join(columns)} FROM main.{relation}"
            if order:
                sql += f" ORDER BY {', '.join(order)}"
            df = con.execute(sql).df()
            # DuckDB hands back microsecond timestamps; transform.py wrote
            # nanosecond ones. Match it so the parquet schema doesn't drift.
            for col in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[col]):
                    df[col] = df[col].dt.as_unit("ns")
            _atomic_write_parquet(df, targets[relation])
            log.info("exported %s: %d rows -> %s", relation, len(df), targets[relation])
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
