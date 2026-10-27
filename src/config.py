"""Shared paths and tunable constants for the Trickle-Down Trend Tracker pipeline."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
BRANDS_YAML = CONFIG_DIR / "brands.yaml"
TRENDS_YAML = CONFIG_DIR / "trends.yaml"
SYNTHETIC_RETAILER_YAML = CONFIG_DIR / "synthetic_retailer.yaml"

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGING_DIR = DATA_DIR / "staging"
MARTS_DIR = DATA_DIR / "marts"
OUTPUT_DIR = DATA_DIR / "output"
CHARTS_DIR = ROOT / "charts"

STAGING_MENTIONS_PATH = STAGING_DIR / "staging_mentions.parquet"
STAGING_BASELINE_PATH = STAGING_DIR / "staging_baseline.parquet"
WEEKLY_BASELINE_PATH = MARTS_DIR / "weekly_baseline.parquet"
WEEKLY_MENTIONS_PATH = MARTS_DIR / "weekly_mentions.parquet"
TREND_DIFFUSION_CSV = OUTPUT_DIR / "trend_diffusion.csv"

# Built by dbt (dbt/profiles.yml points here); src/export_marts.py reads it.
WAREHOUSE_PATH = DATA_DIR / "warehouse.duckdb"

# Fictional retailer's generated data (src/synthetic_retailer.py). Its own
# directory, and its own dbt schema, so it can never mix with real data.
SYNTHETIC_DIR = DATA_DIR / "synthetic"

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MODE = "timelinevolraw"
GDELT_FORMAT = "json"
TIMESPAN = "12m"

# --- GDELT client / rate limiting ---
# The API itself states: "Please limit requests to one every 5 seconds."
# Fail fast rather than hammer a stuck request: the raw cache is durable, so
# a failed task just gets retried cheaply on the next `make ingest` rather
# than needing to be rescued by an enormous in-process retry budget.
MIN_REQUEST_INTERVAL_SECONDS = 5.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 10.0
BACKOFF_JITTER_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0

# ingest_gdelt.py orchestration: give up on the whole run after this many
# consecutive task failures (each already exhausted its own MAX_RETRIES),
# rather than grinding through the remaining tasks one-by-one for nothing.
MAX_CONSECUTIVE_TASK_FAILURES = 3

# Sentinel trend_id used for the brands-only baseline query per tier.
BASELINE_TREND_ID = "_baseline"

# mid_market (footwear & accessories) was added after the original three. It is
# partly a category, not just a price rung, so analyze.py measures it fully but
# keeps it out of the luxury -> semi -> affordable classification.
TIERS = ("luxury", "semi_luxury", "mid_market", "affordable")

# A week whose tier baseline has fewer articles than this gets an undefined
# (NaN) trend_share, same as a zero-baseline week. Found on mid_market, whose
# partial first week held 1 article -- 1 of 1 mentioned suede, a "100%" share
# that is noise, not signal. Mirrored by dbt var min_baseline_articles.
MIN_BASELINE_ARTICLES = 5

# --- Sustained-signal detection (analyze.py) ---
# A week is "elevated" if its 3-week rolling mean share exceeds the trailing
# median share (over the preceding TRAILING_BASELINE_WINDOW_WEEKS weeks) by
# SIGNAL_FACTOR. first_signal_week requires SUSTAINED_WEEKS consecutive
# elevated weeks, so a single stray article can never set a first-signal week.
SIGNAL_ROLLING_WINDOW_WEEKS = 3
TRAILING_BASELINE_WINDOW_WEEKS = 8
SIGNAL_FACTOR = 2.0
SUSTAINED_WEEKS = 2

# --- Planning (src/planning.py) ---
# Runway seasons: Mondays bracketing the show weeks, New York through Paris.
# Approximate -- fashion weeks shift by a few days year to year.
SEASON_SHOWS = {
    "FW26": ("2026-02-02", "2026-03-30"),
    "SS27": ("2026-09-07", "2026-10-05"),
}
# Momentum compares press share in the "early season" (show weeks plus
# POST_SHOW_WEEKS after) against the PRE_SHOW_WEEKS just before the shows.
PRE_SHOW_WEEKS = 8
POST_SHOW_WEEKS = 2
# Ratio at or above RISING is "rising", at or below 1/RISING "fading". 1.5x,
# not 1.1x: a tier's weekly trend counts are small, so modest ratios are noise.
MOMENTUM_RISING_RATIO = 1.5
# Fewer trend mentions than this across both windows -> "too little press".
MIN_MOMENTUM_MENTIONS = 10
# A week whose tier baseline is under this fraction of the tier's median week
# (over both windows) is a collection gap, not quiet press: GDELT lost whole
# days in Sep-Oct 2026 (semi-luxury fell from ~220 articles a week to 0-11).
# Any such week in either window -> "incomplete_data", never a momentum figure.
COMPLETE_WEEK_FRACTION = 0.5
# Buy-plan multiplier = sqrt(momentum), bounded. The square root damps a 4x
# press jump to a 2x buy; the bounds mean one season's press can never move a
# buy more than +50% / -25%. Flat or too-little-press trends get exactly 1.0.
PLAN_MULTIPLIER_MIN = 0.75
PLAN_MULTIPLIER_MAX = 1.5
# Launch timing, relative to the week demand is expected to arrive.
STOCK_LEAD_WEEKS = 1      # stock on shelf one week before demand
MARKETING_LEAD_WEEKS = 2  # campaign starts two weeks before demand

# Forward watchlist: SS27 shows are after the main 12-month fetch ended, so
# the watchlist has its own pinned-window fetch in its own cache directory,
# for both FW26 trends (the in-season view) and SS27 trends (trends.yaml).
# transform.py and dbt glob data/raw/*.json only, so these never mix into the
# main marts. Window = PRE_SHOW_WEEKS before the shows -> the last full week.
WATCHLIST_SEASON = "SS27"
WATCHLIST_RAW_DIR = DATA_DIR / "raw_watchlist"
WATCHLIST_TIMESPAN = "20260713000000-20261025235959"
PLANNING_DIR = OUTPUT_DIR / "planning"

# "In season now": FW26 trends whose press rose in the last IN_SEASON_RECENT_WEEKS
# complete weeks of the watchlist window, against the IN_SEASON_REFERENCE_WEEKS
# before. Same rising/fading thresholds as the post-show momentum.
IN_SEASON_RECENT_WEEKS = 4
IN_SEASON_REFERENCE_WEEKS = 8
