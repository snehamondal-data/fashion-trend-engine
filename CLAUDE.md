# CLAUDE.md

Context for Claude Code sessions in this repo. Read `README.md` for the full write-up and
findings; this file is the working brief.

## What this is

**Trickle-Down Trend Tracker.** Measures how fashion trends diffuse across brand price tiers,
using public news coverage as a proxy for trend momentum.

**Hypothesis:** luxury houses set trends on the runway (Fall/Winter 2026 shows, Feb–Mar 2026),
semi-luxury adapts them next, affordable mass-produces them last — and that lag is measurable in
weeks.

Sole data source: the **GDELT DOC 2.0 API**, `mode=timelinevolraw` — daily article *counts* only.
No headlines, no URLs, no article text; the article-list endpoint is never called, so no
copyrighted content is fetched or stored.

## Architecture / data flow

```
config/*.yaml → ingest_gdelt → data/raw/*.json → transform → data/staging/*.parquet
                                                           → data/marts/*.parquet
                                                              ├→ analyze → data/output/trend_diffusion.csv
                                                              └→ charts  → charts/*.png
```

| Module | Role |
|---|---|
| `src/config.py` | All paths + tunable constants. Change thresholds here, not inline. |
| `src/config_models.py` | Pydantic models + the **only** readers of `config/*.yaml`. |
| `src/query_builder.py` | Pure GDELT query-string construction. No network, no I/O. |
| `src/gdelt_models.py` | Response schemas + the two error types. |
| `src/gdelt_client.py` | **Only** module that makes network calls. Rate limit, retry, cache. |
| `src/ingest_gdelt.py` | Orchestrates 60 tasks: 4 tier baselines + 14 trends × 4 tiers. |
| `src/transform.py` | raw → staging → weekly marts. Full recompute, atomic writes. Kept as the parity reference. |
| `dbt/` | dbt-duckdb port of transform (`make transform-dbt`) + the `synthetic` schema. |
| `src/export_marts.py` | DuckDB warehouse → the same parquet marts, atomically. |
| `src/synthetic_retailer.py` | Sage & Blush (**fictional**) sales/inventory/searches, driven by real `trend_share` with a planted lag. |
| `src/analyze.py` | Sustained-signal detection, tier lags, classification. |
| `src/charts.py` | One PNG per trend, three tier lines each. |
| `src/agent_db.py` | DuckDB views over the marts, parsed SELECT-only guardrail, coverage, query log. |
| `src/agent.py` | NL query agent (Claude tool-calling loop). Reads marts only — never GDELT. |
| `src/agent_eval.py` | Eval suite: fixed questions, ground truth computed from the marts. |
| `src/synthetic_retailer.py` | SIMULATED Sage & Blush data from real share + planted lag(s). |
| `src/planning.py` | Momentum (rising/flat/fading), buy plan, launch timing, backtest, SS27 watchlist. |
| `src/dashboard.py` | marts + planning outputs → `docs/index.html` (GitHub Pages). |

Three layers stay structurally separate:
- **raw** (`data/raw/`) — exactly what GDELT returned, wrapped with fetch provenance. A cache
  file is never rewritten in place.
- **staging** (`data/staging/`) — typed, normalized rows.
- **mart** (`data/marts/`) — weekly aggregates joined into `trend_share`.

## Key design decisions

**Normalize by tier baseline, never raw counts.** Luxury houses get structurally more press than
affordable brands regardless of trend, so raw counts would make luxury the first mover for *every*
trend by construction. Each tier gets a brands-only baseline query; everything downstream runs on
`trend_share = trend_mentions / tier_baseline_mentions`.

**Full recompute for idempotency.** `transform.py` rebuilds staging and both marts from the entire
`data/raw/` directory every run, writing temp-file-then-rename. Nothing is ever appended, so reruns
cannot duplicate — a guarantee by construction, not by a dedup step. This is why `ingested_at` must
come from the raw layer's own `fetched_at` provenance and never from `datetime.now()`: wall-clock
stamping would make two runs over identical inputs differ and break the guarantee.

**Hashed cache keys.** A raw file is `{trend_id}__{tier}__{sha256(query|timespan)[:16]}.json`.
The hash covers the query string, so ingest is resumable by construction and a changed query
fetches fresh rather than silently reusing stale data. ⚠️ The flip side: an edit to
`brands.yaml`/`query_builder.py` **orphans** the old file, which `transform.py` still globs and
double-counts. See Known limitations.

**Fail-fast retries.** GDELT's *effective* throttling is stricter than its stated 5s floor, and
it returns rate-limit messages as plain text under HTTP 200 as often as 429 — so the client
parses and schema-validates **every** body regardless of status code. Two error types with
deliberately different handling:
- `GDELTRateLimitError` (429/5xx/rate-limit text/network) → retry with capped backoff.
- `GDELTResponseError` (non-JSON or schema mismatch, i.e. a real query bug) → **never retried**,
  raises immediately.

Per-request budget is `MAX_RETRIES=3`; the whole run stops after
`MAX_CONSECUTIVE_TASK_FAILURES=3` rather than grinding through the rest for nothing. The raw
cache is durable, so a failed task is just retried cheaply on the next `make ingest`.

**Elevated-signal detection** (not a fixed volume threshold — one viral article must never mark a
trend as emerged). Per `(trend, tier)`, on a dense weekly calendar:
- a week is **elevated** if its 3-week rolling mean share exceeds the trailing 8-week median
  share by `SIGNAL_FACTOR=2.0`;
- `first_signal_week` requires `SUSTAINED_WEEKS=2` consecutive elevated weeks;
- `peak_week` is the max-share week *among elevated weeks*, ties broken earliest;
- classification: `mass` (affordable fired) > `spreading` (semi) > `emerging` (luxury) >
  `no_signal`.

Gap weeks (trend had zero matches) fill to `share=0`; weeks where the tier's *baseline* was zero
stay `NaN` — a genuinely different case, and never an inf or a crash.

**Query agent reads marts only.** `src/agent_db.py` is the agent's sole data path and touches
local parquet/CSV only, so a question can never trigger a GDELT fetch — structural, not a rule.
SQL is validated by *parsing* it with DuckDB's own `json_serialize_sql` (one `SELECT`, nothing
else), never by regex; a second layer blocks filesystem table functions (`read_csv`, `glob`) that
would otherwise be valid SELECTs. Coverage is computed from the marts at startup and injected into
the system prompt, because an empty result set is indistinguishable from a real zero unless you
know that tier was never ingested. The agent must say "press momentum", never "best selling" —
this dataset has no sales data at all. Both behaviours are asserted in `src/agent_eval.py`.
Model is `claude-opus-5-5`; `tool_choice` stays `auto` because Opus 5.5 rejects a forced one.

## How to run

Everything goes through `.venv` — the Makefile calls `.venv/bin/python` directly, so `make`
targets work without activating.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make ingest      # 60 GDELT queries, sequential, ~5s/request floor. Resumable.
make transform   # raw -> staging -> weekly marts
make analyze     # marts -> data/output/trend_diffusion.csv
make charts      # marts -> charts/*.png
make all         # the four above, in order
make test        # pytest, synthetic fixtures only, no network
make clean       # rm staging/marts/output/charts (NOT data/raw — the cache is precious)

make agent Q="'...'"  # ask the marts a question (needs ANTHROPIC_API_KEY)
make eval             # agent eval suite (needs ANTHROPIC_API_KEY)
make eval-dry         # eval cases + ground truth, no API calls
```

`python -m src.ingest_gdelt 5` runs only the first 5 tasks — smoke test before committing to the
full rate-limited sweep.

## Current state

- **All 60 queries cached** (mid_market landed 2026-10-27). 4 tiers; `mid_market` = footwear &
  accessories, measured but excluded from classification. 12 trends `mass`, 2 (`suede`,
  `shearling_texture`) `spreading`.
- Everything runs clean; `make dashboard` writes `docs/index.html` (GitHub Pages). **117 tests pass**
  (run under `arch -x86_64`: the venv is Intel).
- dbt port matches `transform.py` exactly (values + dtypes); `trend_diffusion.csv` byte-identical.
- Synthetic layer: the 6-week planted lag is recovered for 14/14 trends on searches at the semi-luxury
  tier (median r 0.96), each clearing its own per-product decoy ceiling (0.53–0.84). Sales recover
  it less cleanly (stockouts cap them) — by design. Validation: mixed delays 3/3, noisy store confident
  on 5/14 (all right), null 2/42 false positives.
- The orphaned luxury-baseline cache file has been deleted and the marts/charts rebuilt; luxury
  `trend_share` is now correct (it was exactly half). Lag table was unaffected either way.
- README Findings regenerated 2026-10-27 on all 60 queries. The old semi→affordable claim is
  explicitly withdrawn (6–6 split). Headline: suede sits at a median 13.3% of mid-market coverage vs
  ~1.6% elsewhere but never "breaks out" — breakout detection can't see always-on trends.

## Planning layer (added 2026-10-27)

- **Seasons in trends.yaml**: `season: FW26` (default) or `SS27`. `TrendsConfig.from_yaml()` returns FW26
  only, so the main pipeline/Sage & Blush/agent never see SS27. The 8 SS27 trends are sourced (named
  houses + URL); don't add unsourced ones. `season=None` loads all (watchlist ingest only).
- **In season now** (`planning.in_season`): FW26 trends, last 4 vs prior 8 weeks of the watchlist window,
  demand window = recent weeks + measured (simulated) delay. Unfetched (trend, tier) pairs are `no_data`.
- **GDELT gap weeks (Sep–Oct 2026)**: whole days missing (semi-luxury baseline 0–11 articles in weeks of
  Sep 14 and Oct 5 vs ~220 normal; mid_market in the main data too). Any week under
  `COMPLETE_WEEK_FRACTION` (0.5) of the tier's median in either window → `incomplete_data`, multiplier
  1.0. As of 2026-10-27 every SS27 and in-season reading is incomplete; they'd need a later window.
- **Watchlist fetch** is separate: `make ingest-watchlist` → `data/raw_watchlist/` (92 queries), pinned window
  `WATCHLIST_TIMESPAN` (a `YYYYMMDDHHMMSS-YYYYMMDDHHMMSS` timespan → GDELT start/enddatetime, part
  of the cache key). Never put these files in `data/raw/`: transform/dbt glob it and would sum them.
  Status 2026-10-29: all 92/92 cached (the last 24 came via a phone hotspot after GDELT throttled
  the home network). Every reading is still `incomplete_data` because of the Sep–Oct gap weeks.
- **Noise ceiling is per product**: own trend must beat the 13 decoy trends' press (all tiers/lags)
  and the core skus. The old core-only ceiling was too low for spiky demand; the null test caught it.
  Measured false-positive rate ~3% per product. Don't go back to a core-only ceiling.
- `demand_truth.parquet` is simulation ground truth, read only by the planning backtest, never dbt.
- Backtest numbers (−14% buy error, no change in sold-out weeks) are findings, not asserted in tests.
- Planning must only use information available at the decision week (`breakout_week` takes `until`).

## Conventions

- **Contract-first.** Pydantic models at every boundary (config, API response, cache). Define the
  schema before the code that fills it.
- **Fail loudly.** Malformed config, non-JSON responses, schema mismatches raise immediately with
  the offending value in the message. Never silently coerce, never swallow, never return a
  partial result that looks complete.
- **Isolate the network.** `gdelt_client.py` is the only module that touches the wire.
  `query_builder.py` is deliberately pure so query construction is trivially testable.
- **Tests cover transform and lag logic** — the two places a silent wrong answer is plausible.
  Synthetic fixtures only, no network. Any change to signal detection or weekly aggregation needs
  a test alongside it.
- **Never commit `data/` or `charts/`** (both gitignored). Representative charts for the README
  are copied into `docs/`, which *is* tracked.
- Comments explain *why*, especially where the reason is a live-API quirk that the docs don't
  mention. Keep that habit — several non-obvious behaviors are only recorded there.

## Known limitations

- **GDELT throttling** is the main operational constraint. Effective limits are stricter than the
  stated one-request-per-5s, individual requests sometimes need minutes of retrying, and heavy
  testing once triggered what looked like an extended soft-block on one IP (a different network
  cleared it immediately). Throttled tasks just need `make ingest` rerun later.
- **Shifted windows.** `timespan=12m` is relative to fetch time and the cache key omits it, so
  mid_market (fetched a month later) covers Oct 2025–Oct 2026 vs Sep 2025–Sep 2026 for the rest,
  and refetching reuses stale files. Fix: explicit STARTDATETIME/ENDDATETIME in query + cache key.
- **Mid-market is thin**: median 28 baseline articles/week (vs 192 semi, 882 affordable, 2,800
  luxury). Its lags are low-confidence. Weeks with < MIN_BASELINE_ARTICLES (5) brand articles get
  a NaN share (its partial first week had 1 article = a bogus 100%); that rule moved mid_market lags
  for 5 trends and changed nothing in the other three tiers.
- **Luxury→semi lag inversion.** For 3 of 5 `mass` trends the semi-luxury signal fires *before*
  luxury's, and for 2 more luxury never fires. Read literally that contradicts the hypothesis. The
  likeliest explanation is a real limitation of a rolling same-series baseline: FW26 luxury
  coverage is already "priced in" to luxury's own trailing median, so no fresh breakout registers
  even when the trend is genuinely luxury-led. Partial-ingest gaps are a second candidate.
  Distinguishing these needs a second season. (The semi→affordable lag once looked solid on 7
  trends; on all 14 it splits 6–6 and the claim is withdrawn.)
- **Orphaned cache files** (fixed once, now guarded). Any edit to `brands.yaml` or the query
  builder leaves the old raw file beside the new one; the pandas transform would sum both (it once
  halved every luxury `trend_share`). The dbt build now fails on it
  (`assert_one_raw_file_per_trend_tier`): move the stale file out of `data/raw/` and rebuild.
- **Press attention ≠ product adoption.** A trend "reaching" affordable means affordable-brand
  press started mentioning it, not that anything shipped. The Shopify `/products.json` step in
  Next Steps is the real fix.
- **"The Row" is unresolved.** No disambiguating `search_term` exists that doesn't gut recall, so
  it stays a documented noise source in the luxury baseline.
- **Fashion-week weeks inflate luxury coverage** across the board. Baseline normalization
  mitigates but doesn't eliminate this; charts shade the FW26 window for this reason.
