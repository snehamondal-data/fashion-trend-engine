"""Real weekly marts + config/synthetic_retailer.yaml -> synthetic retail data
for Sage & Blush, a FICTIONAL semi-luxury retailer.

Purpose: a known-answer test for the demand-overlay analysis. Demand for each
trend-tagged product is driven by its trend's real trend_share, delayed by
planted_lag_weeks; if the analysis can't recover that lag here, it can't be
trusted on a real retailer's data. Core products ignore trends entirely and
are the negative control.

Two deliberate realism choices, because they're what make the analysis
non-trivial:
  - sales are censored by stock: units_sold = min(demand, on hand). A trend
    spike sells core sizes out, and sales then *understate* demand.
  - searches are not censored: shoppers search for a sold-out item anyway.
    So site searches, not sales, are the cleaner demand signal.

Output is deterministic (seeded; manifest records input hashes, never
wall-clock time), so reruns over the same inputs produce identical files --
the same idempotency guarantee as the transform.
"""
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.analyze import share_series, tier_calendar
from src.config import SYNTHETIC_DIR, SYNTHETIC_RETAILER_YAML, WEEKLY_BASELINE_PATH, WEEKLY_MENTIONS_PATH
from src.config_models import SyntheticRetailerConfig, TrendsConfig
from src.transform import _atomic_write_parquet

log = logging.getLogger("synthetic_retailer")

PRODUCTS_FILE = "products.parquet"
INVENTORY_FILE = "inventory_weekly.parquet"
SEARCHES_FILE = "site_searches.parquet"
MANIFEST_FILE = "manifest.json"


def trend_index(share: pd.Series, lag_weeks: int) -> pd.Series:
    """share / its own mean, delayed by lag_weeks. 1.0 = an ordinary week.

    Dividing by the trend's own mean puts trends with very different press
    volume on one scale. Weeks with no lagged value yet (the first lag_weeks)
    or an undefined share (tier baseline was 0) are neutral, not zero."""
    mean = share.mean(skipna=True)
    if not mean or np.isnan(mean):
        return pd.Series(1.0, index=share.index)
    return (share / mean).shift(lag_weeks).fillna(1.0)


def generate(
    cfg: SyntheticRetailerConfig,
    drivers: Dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    trends: TrendsConfig,
) -> Dict[str, pd.DataFrame]:
    """Pure: no I/O. `drivers` maps trend_id -> weekly trend_share on `calendar`
    (un-lagged; the planted lag is applied here)."""
    rng = np.random.default_rng(cfg.seed)
    sizes = list(cfg.size_curve)
    weights = np.array([cfg.size_curve[s] for s in sizes])
    first_term = {t.id: t.query_terms[0] for t in trends.trends}
    neutral = pd.Series(1.0, index=calendar)

    inventory_rows, search_rows = [], []
    for p in cfg.products:
        index = trend_index(drivers[p.trend_id], cfg.planted_lag_weeks) if p.trend_id else neutral
        multiplier = np.maximum(cfg.demand.floor, 1.0 + cfg.demand.beta * (index.to_numpy() - 1.0))
        demand_total = rng.poisson(p.base_weekly_units * multiplier)

        # Inventory per size: start at par, top back up to par every N weeks.
        par = {s: math.ceil(p.base_weekly_units * cfg.size_curve[s] * cfg.inventory.par_weeks_of_cover) for s in sizes}
        on_hand = dict(par)
        terms = [first_term[p.trend_id], p.search_term] if p.trend_id else [p.search_term]

        for i, week in enumerate(calendar):
            by_size = rng.multinomial(demand_total[i], weights)
            replenish = i > 0 and i % cfg.inventory.replenish_every_weeks == 0
            for size, demanded in zip(sizes, by_size):
                start = on_hand[size]
                received = par[size] - start if replenish else 0
                sold = min(int(demanded), start + received)
                # Returns are counted (they hit net revenue) but not restocked:
                # a simplification, so on_hand_end = start + received - sold holds exactly.
                returned = int(rng.binomial(sold, cfg.demand.return_rate))
                on_hand[size] = start + received - sold
                inventory_rows.append(
                    {
                        "week_start": week,
                        "sku_id": p.sku_id,
                        "size": size,
                        "on_hand_start": start,
                        "units_received": received,
                        "units_sold": sold,
                        "units_returned": returned,
                        "on_hand_end": on_hand[size],
                    }
                )

            searches = rng.poisson(cfg.demand.searches_per_unit * demand_total[i])
            for term, n in zip(terms, rng.multinomial(searches, [1 / len(terms)] * len(terms))):
                search_rows.append({"week_start": week, "sku_id": p.sku_id, "search_term": term, "searches": int(n)})

    products = pd.DataFrame([{"retailer": cfg.retailer_name, **p.model_dump()} for p in cfg.products])
    return {
        PRODUCTS_FILE: products,
        INVENTORY_FILE: pd.DataFrame(inventory_rows),
        SEARCHES_FILE: pd.DataFrame(search_rows),
    }


def drivers_from_marts(
    cfg: SyntheticRetailerConfig, weekly_mentions_df: pd.DataFrame, weekly_baseline_df: pd.DataFrame
) -> tuple:
    calendar = tier_calendar(weekly_baseline_df, cfg.driver_tier)
    trend_ids = sorted({p.trend_id for p in cfg.products if p.trend_id})
    drivers = {t: share_series(weekly_mentions_df, t, cfg.driver_tier, calendar) for t in trend_ids}
    return drivers, calendar


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    config_path: Path = SYNTHETIC_RETAILER_YAML,
    weekly_mentions_path: Path = WEEKLY_MENTIONS_PATH,
    weekly_baseline_path: Path = WEEKLY_BASELINE_PATH,
    out_dir: Path = SYNTHETIC_DIR,
    trends: Optional[TrendsConfig] = None,
) -> Dict[str, pd.DataFrame]:
    trends = trends or TrendsConfig.from_yaml()
    cfg = SyntheticRetailerConfig.from_yaml(config_path, trends=trends)
    drivers, calendar = drivers_from_marts(
        cfg, pd.read_parquet(weekly_mentions_path), pd.read_parquet(weekly_baseline_path)
    )
    if calendar.empty:
        raise ValueError(f"No {cfg.driver_tier} baseline weeks in {weekly_baseline_path}; run the transform first")

    frames = generate(cfg, drivers, calendar, trends)
    for name, df in frames.items():
        _atomic_write_parquet(df, out_dir / name)

    manifest = {
        "synthetic": True,
        "retailer": cfg.retailer_name,
        "disclaimer": cfg.disclaimer,
        "seed": cfg.seed,
        "driver_tier": cfg.driver_tier,
        "planted_lag_weeks": cfg.planted_lag_weeks,
        "config_sha256": _sha256(config_path),
        "weekly_mentions_sha256": _sha256(weekly_mentions_path),
        "weeks": len(calendar),
    }
    tmp = out_dir / (MANIFEST_FILE + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(out_dir / MANIFEST_FILE)

    log.info(
        "generated %s (synthetic): %d products, %d inventory rows, %d search rows over %d weeks -> %s",
        cfg.retailer_name, len(frames[PRODUCTS_FILE]), len(frames[INVENTORY_FILE]),
        len(frames[SEARCHES_FILE]), len(calendar), out_dir,
    )
    return frames


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
