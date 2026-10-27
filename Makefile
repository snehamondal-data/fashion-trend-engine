.PHONY: ingest ingest-watchlist transform transform-dbt synthetic planning dashboard analyze charts all test clean agent eval eval-dry

VENV := .venv/bin

ingest:
	$(VENV)/python -m src.ingest_gdelt

# SS27 forward watchlist: same 60 queries, pinned to the window around the
# SS27 shows, cached separately in data/raw_watchlist/. Resumable.
ingest-watchlist:
	$(VENV)/python -m src.ingest_gdelt --watchlist

transform:
	$(VENV)/python -m src.transform

# dbt port of `transform`: builds data/warehouse.duckdb, runs the data tests
# (orphaned cache files fail the build), then exports the same parquet marts.
# tests/test_dbt_parity.py asserts its output is identical to `transform`.
transform-dbt:
	$(VENV)/dbt build --project-dir dbt --profiles-dir dbt --exclude tag:synthetic
	$(VENV)/python -m src.export_marts

# SYNTHETIC data for Sage & Blush, a fictional retailer, generated from the
# real marts with a planted lag (config/synthetic_retailer.yaml). Builds into
# its own `synthetic` schema. Run after transform-dbt.
synthetic:
	$(VENV)/python -m src.synthetic_retailer
	$(VENV)/dbt build --project-dir dbt --profiles-dir dbt --select tag:synthetic

# Momentum, buy plan, launch timing, backtest (simulated) and the SS27
# watchlist (real press) -> data/output/planning/. Run after synthetic.
planning:
	$(VENV)/python -m src.planning

# Static dashboard for GitHub Pages: marts + warehouse -> docs/index.html.
# Run after analyze and planning.
dashboard:
	$(VENV)/python -m src.dashboard

analyze:
	$(VENV)/python -m src.analyze

charts:
	$(VENV)/python -m src.charts

all: ingest transform analyze charts

# Ask the marts a question. Needs ANTHROPIC_API_KEY; never calls GDELT.
#   make agent Q="which trends reached the affordable tier fastest?"
agent:
	$(VENV)/python -m src.agent --show-sql $(Q)

# Eval suite. `eval-dry` prints the cases and their ground truth with no API calls.
eval:
	$(VENV)/python -m src.agent_eval

eval-dry:
	$(VENV)/python -m src.agent_eval --dry-run

test:
	$(VENV)/python -m pytest -q

clean:
	rm -rf data/staging data/marts data/output charts
