# Trickle-Down Trend Tracker

A fashion trend diffusion engine. **Hypothesis:** luxury houses set trends on the runway
(Fall/Winter 2026 shows, Feb–Mar 2026); semi-luxury brands adapt them next; affordable brands
mass-produce them last. This project measures that lag using public news coverage as a proxy
for trend momentum — not sales data, not social media, just how much the press is talking about
a given trend in the context of a given brand tier, and how many weeks it takes for that
attention to move from one tier to the next.

**[Open the interactive dashboard →](https://snehamondal-data.github.io/fashion-trend-engine/)**
Press momentum per trend and tier, the lag table, and the synthetic-retailer demand test, on one
page. Built from the marts by `make dashboard`.

## Architecture

```mermaid
flowchart TB
    subgraph SRC["Sources"]
        G["GDELT DOC 2.0 API<br/>daily article counts only"]
        CFG["config/brands.yaml<br/>config/trends.yaml"]
    end

    subgraph EXT["Extract (Python)"]
        I["ingest_gdelt.py + gdelt_client.py<br/>rate limit, retry, validate every body"]
    end

    subgraph BRONZE["Bronze: data/raw"]
        R[("cached JSON per query<br/>+ fetched_at provenance<br/>never rewritten")]
    end

    subgraph DBT["Transform: dbt-duckdb, full rebuild every run"]
        S["Silver: staging views<br/>stg_gdelt__timeline_points<br/>stg_gdelt__mentions / baseline"]
        WB["Gold: fct_weekly_baseline"]
        WM["Gold: fct_weekly_mentions<br/>trend_share = mentions / tier baseline"]
        Q{{"data tests: grain,<br/>one raw file per trend x tier"}}
    end

    subgraph SERVE["Analyze and serve"]
        A["analyze.py<br/>sustained signal, tier lags"]
        CSV[("trend_diffusion.csv")]
        CH["charts.py: one PNG per trend"]
        AG["NL query agent<br/>Claude + SELECT-only guardrail"]
        DASH["Streamlit dashboard"]
    end

    subgraph SYN["Synthetic retailer layer (synthetic_ prefix, never mixed with real)"]
        GEN["generator seeded from trend_share<br/>with a planted lag"]
        SD[("synthetic catalog, sales,<br/>on-hand by size, site searches")]
        OV["demand overlay mart<br/>+ lag-recovery test"]
    end

    CFG --> I
    I <-->|"5s floor, backoff on 429"| G
    I --> R
    R --> S
    S --> WB
    S --> WM
    WB --> WM
    Q -.- S
    WM -->|"export_marts.py<br/>parquet, atomic write"| A
    A --> CSV
    WM --> CH
    WM --> AG
    CSV --> AG
    WM -.-> GEN
    GEN -.-> SD
    SD -.-> OV
    WM -.-> OV
    OV -.-> DASH
    WM -.-> DASH

    classDef planned stroke-dasharray: 5 5
    class GEN,SD,OV,DASH planned
```

Dashed boxes and arrows are **planned** (a synthetic semi-luxury retailer's demand data, in progress). Everything
solid runs today.

Medallion layers, kept structurally separate:
- **Bronze** (`data/raw/`) — exactly what GDELT returned, wrapped with fetch provenance. Append-only;
  a cache file is never rewritten in place.
- **Silver** (dbt staging views → `data/staging/`) — typed, normalized rows: one per
  `(trend_id | tier, date)`. The raw JSON schema is declared, not inferred.
- **Gold** (dbt mart tables → `data/marts/`) — weekly aggregates joined into `trend_share` (see
  below); `analyze.py` builds lags and classifications on top.
- **Synthetic** (planned) — a fictional semi-luxury retailer's mock sales data lives under its own `synthetic_` prefix so it
  can never be joined to real data by accident.

The transform runs in dbt (`make transform-dbt`); `tests/test_dbt_parity.py` asserts it produces
output identical to the original Python `transform.py`, which is kept for now. Both fully
recompute staging and both marts from whatever is currently in `data/raw/` on every run, and
write atomically (temp file + rename). Nothing is ever appended to an existing
output, so a rerun cannot create duplicates — that guarantee holds by construction, not by a
dedup step.

## Data sources & legal notes

The only external data source is the [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/),
specifically `mode=timelinevolraw`: **daily article counts** matching a boolean keyword query,
plus `norm` (total articles GDELT monitored that day, for normalization). This endpoint returns
**counts only** — no headlines, no URLs, no article text. This project never calls GDELT's
article-list endpoint, so no copyrighted article content is ever fetched or stored; `data/raw/`
contains nothing beyond aggregate mention counts and the query that produced them.

Two things learned by hitting the live API directly (not documented in GDELT's own docs):

1. **`Parentheses may only be used around OR'd statements.`** A single search term must be bare;
   only a group of 2+ terms joined by `OR` may be wrapped in `()`.
2. **A bare term with a "special" character is rejected as an "illegal character."** A bare
   `H&M` failed; GDELT's own error message says to quote it instead (its example is a dash,
   `"f-16"`). `query_builder.py` quotes any term that isn't plain alphanumeric, not just
   multi-word phrases.
3. **Rate limit, stated by the API itself:** *"Please limit requests to one every 5 seconds."*
   This came back as plain text, sometimes under HTTP 429, sometimes under HTTP 200 — so the
   client never trusts status codes alone; every response body is parsed and schema-validated
   regardless of status. In practice, under sustained use, the *effective* throttling was
   noticeably stricter than that stated floor — individual requests sometimes needed several
   minutes of retrying, and the pipeline hit what looked like an extended soft-block on one IP
   during heavy testing (a different network cleared it immediately). `ingest_gdelt.py` is
   written assuming this will happen again: it fails fast per-request (`MAX_RETRIES=3`, capped
   backoff) rather than retrying forever, stops the whole run after
   `MAX_CONSECUTIVE_TASK_FAILURES` (default 3) failures in a row, and is fully resumable — the
   raw cache is durable, so a rerun only fetches what's still missing and reports a cached/
   fetched/failed summary at the end.

## Design decisions & trade-offs

**Normalize by tier baseline, not raw counts.** Luxury houses (Chanel, Hermès) get structurally
more press than affordable brands (Mango, Uniqlo) regardless of any specific trend — so raw
mention counts would make luxury look like the first mover for *every* trend, by construction.
Three baseline queries (brands OR'd, no trend terms) are fetched per tier, and every trend's
weekly mentions are expressed as `trend_share = trend_mentions / tier_baseline_mentions`. All
signal detection, lag calculation, and charts run on `trend_share`; raw `mention_count` stays in
`weekly_mentions.parquet` for reference.

**Sustained signal, not a fixed volume threshold.** A single viral article shouldn't be able to
mark a trend as "emerged." Per `(trend, tier)`, a week counts as `elevated` only if its 3-week
rolling mean share exceeds the trailing 8-week median share by 2x (`SIGNAL_FACTOR`, in
`src/config.py`), and `first_signal_week` requires 2 consecutive (`SUSTAINED_WEEKS`) elevated
weeks. One caveat this produces: if an elevated period runs long enough (several weeks), the
trailing median itself eventually rises to include those elevated weeks, and the plateau can stop
being flagged as "elevated" even though coverage hasn't dropped — the algorithm is tuned to catch
the *onset* of a breakout, not to track every week of a long-sustained trend. `peak_week` is
therefore the max-share week among weeks the algorithm *did* flag as elevated (ties broken by the
earliest such week), which can under-represent a true peak that arrives late in a long plateau.

**Ambiguous brand names get a disambiguating `search_term`.** "Theory", "Reformation", "COS", and
"Mango" are common words/acronyms that would pull in unrelated news under a bare-name query.
`config/brands.yaml` lets a brand entry be `{name, search_term}` instead of a plain string (e.g.
`Theory` → `"Theory clothing"`); `search_term` defaults to `name` when not given. This is a
precision-over-recall call: some true-positive mentions that don't happen to include the
disambiguating word are lost, in exchange for far less noise. **"The Row" stays unresolved** — no
good disambiguating term exists for it without meaningfully hurting recall, so it's a known,
documented source of noise in the luxury baseline and any trend involving that brand.

**Full-recompute idempotency over incremental upsert.** `transform.py` always rebuilds staging
and both marts from the entire `data/raw/` directory rather than merging new rows into existing
parquet files. Simpler than upsert/merge logic, and correct at this data scale (14 trends × 4
tiers + 4 baselines = 60 raw files).

**Provenance on every row.** Each raw cache file wraps the GDELT response with `query`,
`timespan`, `url`, and `fetched_at` (UTC). `fetched_at` is written once, at actual fetch time, and
carried through unchanged as `ingested_at` on every staging row sourced from that file —
`transform.py` never stamps `datetime.now()` itself, since doing so would make two runs over
identical raw inputs produce different output and break the idempotency guarantee.

## Limitations

- **Fashion-week weeks inflate luxury coverage regardless of trend.** Feb–Mar 2026 (the FW26 show
  season) is a spike in luxury press volume across the board — including for brands/trends that
  have nothing to do with a given query. Baseline normalization (`trend_share`) mitigates this
  (a trend's share of *that week's* elevated luxury coverage, not raw counts), but doesn't
  eliminate it entirely; charts shade the FW26 show window for this reason.
- **News volume measures press attention, not product adoption.** A trend "reaching" the
  affordable tier here means affordable-brand press started mentioning it — not that affordable
  retailers actually shipped the product. That's exactly the gap the Shopify `/products.json` step
  below is meant to close.
- **"The Row" brand-name collision** (see above) is a known, unresolved noise source.
- **No documented GDELT query-length cap** was found; the luxury brand OR-block alone runs
  ~160 characters. Not pre-building query-chunking for a limit that isn't confirmed to exist — if
  it surfaces, the client's fail-loud JSON validation will point at the exact `(trend, tier)` pair
  responsible.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make ingest         # fetch + cache all 60 GDELT queries (sequential, ~5s/request minimum)
make transform-dbt  # raw -> staging -> weekly marts in dbt, data tests, parquet export
make analyze        # weekly marts -> data/output/trend_diffusion.csv
make charts         # weekly marts -> charts/*.png (one line per tier)
make synthetic      # Sage & Blush synthetic retail data + lag-recovery marts (dbt `synthetic` schema)
make dashboard      # everything above -> docs/index.html (the GitHub Pages dashboard)
make transform      # the original pandas transform; kept as the dbt parity reference
make all            # ingest, transform, analyze, charts
make test        # pytest -- all synthetic fixtures, no network calls

make agent Q="'which trends reached the affordable tier fastest?'"   # ask the marts
make eval        # agent eval suite (needs ANTHROPIC_API_KEY)
make eval-dry    # print eval cases + ground truth, no API calls
```

Each module is also runnable standalone, e.g. `python -m src.ingest_gdelt` (optionally
`python -m src.ingest_gdelt 5` to run only the first 5 tasks — useful for a smoke test before
committing to the full 60-query, rate-limited sweep).

## Query agent

A natural-language layer over the finished marts: ask a question, get an answer
grounded in a SQL query it actually ran.

```bash
make agent Q="'what is the lag for suede?'"
```

**DuckDB reads the parquet marts directly** — `weekly_mentions`, `weekly_baseline`, and
`trend_diffusion` are registered as views over the files the pipeline already produced. The agent
has no network path to GDELT at all: `src/agent_db.py` is its only data access, and it reads local
files and nothing else, so a question can never trigger a fetch or cost an API quota.

**The SQL guardrail parses, it doesn't pattern-match.** Candidate SQL goes through DuckDB's own
parser (`json_serialize_sql`) and must come back as exactly one `SELECT`. Stacked statements
(`SELECT 1; DROP TABLE ...`), DDL, and DML fail to parse at all. A second layer rejects
filesystem-reaching table functions (`read_csv`, `read_parquet`, `glob`), which would otherwise be
valid SELECTs. Every query is logged to `data/output/agent_queries.jsonl` with its question, row
count, duration, and any error.

Two behaviours are the actual point, and both are asserted in the eval suite rather than just
described in the prompt:

- **It refuses past its data.** Coverage is computed from the marts at startup and injected into
  the system prompt, because an empty result set is indistinguishable from a real zero unless you
  already know that tier was never ingested. Asked about a trend missing a tier, it names the
  missing tier and declines to give a lag — `trend_diffusion` still has a row for such trends, and
  those values are artefacts of missing data, not findings.
- **It describes the metric honestly.** This measures press coverage volume, so the agent says
  "press momentum" or "media attention" and never "best selling", "sell-through", or a bare
  "trending". Asked a sell-through question outright, it says the dataset cannot answer it instead
  of substituting press volume.

`make eval` runs the suite: fixed questions, with the expected values computed from the marts by
ground-truth SQL at run time rather than pasted in as literals, since incremental ingest would rot
hard-coded numbers into false failures. `make eval-dry` prints the cases and their ground truth
without spending any tokens.

## Synthetic retailer test (Sage & Blush)

The business question behind this project is whether a retailer could use press momentum as an
early demand signal. Real sales data isn't available, so the method is tested on a **fictional**
semi-luxury retailer, Sage & Blush, whose answer is known in advance.

- **Generated from real data, with a planted delay.** Each of 14 products is tied to one trend
  (a suede wrap skirt, an oxblood slip skirt...). Its weekly demand follows that trend's real
  semi-luxury `trend_share`, delayed by **6 weeks**, plus noise
  (`config/synthetic_retailer.yaml`, `src/synthetic_retailer.py`).
- **Realistic in the way that matters.** Sales are capped by stock on hand, so a trend spike sells
  out core sizes and sales then *understate* demand. Site searches aren't capped.
- **A negative control.** Four core products (a bodysuit, a poplin shirt...) ignore trends
  entirely. Testing them against every trend, tier and delay shows how strong a correlation
  chance alone produces.
- **The analysis isn't told the delay.** `mart_synthetic__lag_correlation` correlates press in
  week *t* with demand in week *t + k* for k = 0–12, for every tier;
  `mart_synthetic__recovered_lag` picks the best k.

**Result:** the 6-week delay is recovered for all 14 trends from searches, at the semi-luxury tier
(median r 0.96), well above the 0.53 that the core products reached by chance across 2,184 tries.
The other tiers mostly don't clear that ceiling, so the method also finds *which* tier drives
demand. Using sales instead of searches, some trends point to the wrong delay: stockouts cap
sales exactly when demand peaks, which is why search data is the better early signal.

What this does and doesn't show: it proves the method *can* find a delay when one exists. The
synthetic world is cleaner than a real one (correlations near 1), so it says nothing about how
any real retailer's demand behaves. That needs real data. All synthetic data is labelled as such,
lives in its own `synthetic` dbt schema, and is generated deterministically (seeded, with input
hashes in a manifest). `tests/test_synthetic_retailer.py` plants a 4-week delay end to end, so
nothing passes by having the answer hard-coded.

## Findings

All 60 GDELT queries are in: 14 trends × 4 tiers, plus a baseline per tier. Per-trend numbers,
charts and the lag table are on the **[dashboard](https://snehamondal-data.github.io/fashion-trend-engine/)**
and in `data/output/trend_diffusion.csv`. Lags are weeks between one tier's first *sustained*
breakout and the next tier's; positive means the cheaper tier followed.

| Step | Trends with a lag | Cheaper tier followed | Cheaper tier was first | Median lag |
|---|---|---|---|---|
| Luxury → semi-luxury | 9 | 3 | **6** | −6 weeks |
| Semi-luxury → affordable | 12 | 6 | 6 | +2.5 weeks |
| Semi-luxury → mid-market shoes & bags | 10 | 7 | 3 | +8 weeks |

12 of 14 trends reached a sustained breakout in affordable-brand press within the year; `suede` and
`shearling_texture` stopped at semi-luxury.

**1. Semi-luxury press usually picked a trend up before luxury press did.** That is the reverse
of the trickle-down story, and it held from the first 7 trends through all 14. I don't read it as
"semi-luxury leads". The likelier cause is a limit of the method: luxury coverage is enormous and
constant (a median of ~2,800 brand articles a week), so a new trend barely moves luxury's share,
while the same trend is a visible jump in semi-luxury's much smaller coverage. Breakout detection
compares a tier against its own recent past, so it finds fresh spikes where coverage is thinnest.

**2. There is no consistent order between semi-luxury and affordable.** An earlier version of
this README, written on 7 trends, reported a strong semi→affordable lag (+8 to +36 weeks). With
all 14 trends it splits exactly 6–6. That claim is withdrawn.

**3. Suede didn't skip the cheaper tiers. It went to shoes and bags, and stayed.** The pipeline
classifies suede as never reaching affordable brands. But in mid-market footwear and accessories
coverage (Dolce Vita, Tony Bianco, Steve Madden, Sam Edelman, Coach, Kate Spade), suede appears in
a median **13.3%** of weekly brand coverage, against about **1.6%** in every other tier, and it's
there almost every week. It never registers as a breakout because there's nothing to break out
from: breakout detection can't see a trend that's always on. This came from checking the output
against what's visibly in stores, and it names a real blind spot: **breakout is not presence.**
The same blind spot likely explains finding 1.

The mid-market tier behaves like a footwear-and-bags segment should: materials and prints rank high
(suede, sheer, shearling, leopard), while clothing silhouettes barely register (tuxedo dressing and
the skirt suit are each ~0.2%).

![Suede mention share by tier](docs/suede.png)

**How much to trust this:**
- **One season.** Twelve months of data can't separate a pattern from a one-off.
- **Mid-market is thin.** A median of 28 brand articles a week (precise search phrases like
  "Coach handbag" avoid noise but miss coverage). Weeks with fewer than 5 brand articles get no
  share at all (`MIN_BASELINE_ARTICLES`; its partial first week held one article, a meaningless
  "100%"). Adding that rule changed no luxury, semi-luxury or affordable result but moved the
  mid-market lags for 5 trends, which is the measured case for treating them as low-confidence.
  The suede share difference is large enough to survive it (median 13.3% either way).
- **Shifted windows.** GDELT's `timespan=12m` counts back from the fetch date, and the cache key
  doesn't include that date. Mid-market was fetched a month after the other tiers, so it covers
  late Oct 2025 – late Oct 2026 while the others cover late Sep 2025 – mid Sep 2026.
- **Press is not product.** A tier "reaching" a trend means its brand press started mentioning it.

## Next steps

- **Shopify `/products.json` catalogs** — query affordable/semi-luxury retailers' public product
  catalogs directly to measure *actual product listings*, not just press mentions. This is the
  real fix for the "press attention vs. product adoption" gap noted above.
- **LLM trend extraction from runway coverage** — replace the hand-curated `trends.yaml` with an
  LLM pass over runway review articles, so new seasons don't require manually re-deriving trend
  terms and evidence.
- **BigQuery + Dataform** — move the mart layer into BigQuery with Dataform-managed
  transformations, for a real warehouse instead of local parquet, and to make this pipeline
  trivially schedulable.
- **Pin explicit date windows.** Request `STARTDATETIME`/`ENDDATETIME` instead of `timespan=12m`,
  and include them in the cache key, so every tier covers the same weeks regardless of fetch date.
  Drop partial edge weeks at the same time.
- **Presence, not just breakout.** Report each trend's steady share per tier alongside breakout
  timing, so always-on trends like suede in footwear are visible in the results, not only in a
  follow-up check.
- **A second season.** One season can't separate a real pattern from a one-off; FW25 coverage
  would give every trend a "normal year" to compare against.
- **Material-swap terms for the cheaper tiers.** Trickle-down often changes the material, not just
  the price: luxury suede becomes faux or "vegan" suede at mass price points. Tracking those terms
  per tier would catch diffusion the current single-term queries miss.
- **A real retailer's search and sales data.** The synthetic test shows the method works when a
  delay exists; joining a real retailer's site-search and sell-through data is the actual test.
- **Consumer search (Google Trends) alongside press.** Press says what editors cover; search says
  what shoppers want. The gap between them is its own signal.
