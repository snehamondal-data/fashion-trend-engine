from pathlib import Path

import pytest

DBT_PROJECT = Path(__file__).resolve().parent.parent / "dbt"


@pytest.fixture
def run_dbt(tmp_path, monkeypatch):
    """Run `dbt build` in-process against a throwaway DuckDB file.
    Returns (dbtRunnerResult, warehouse_path)."""
    dbt_main = pytest.importorskip("dbt.cli.main")

    def _run(dbt_vars: dict, exclude: str = None):
        warehouse = tmp_path / "warehouse.duckdb"
        monkeypatch.setenv("TREND_DUCKDB_PATH", str(warehouse))
        args = [
            "build",
            "--project-dir", str(DBT_PROJECT),
            "--profiles-dir", str(DBT_PROJECT),
            "--target-path", str(tmp_path / "target"),
            "--log-path", str(tmp_path / "logs"),
            # Partial parse caches env_var() values; a stale warehouse path
            # from a previous test would silently be reused.
            "--no-partial-parse",
            "--quiet",
            "--vars", "{" + ", ".join(f"{k}: '{v}'" for k, v in dbt_vars.items()) + "}",
        ]
        if exclude:
            args += ["--exclude", exclude]
        return dbt_main.dbtRunner().invoke(args), warehouse

    return _run
