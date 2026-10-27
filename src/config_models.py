"""Pydantic models + loaders for config/brands.yaml and config/trends.yaml.

These are the only functions that should read the config files: everything
downstream works against validated, normalized Python objects, and any
malformed config raises immediately (fail loud) rather than propagating
silently into a query or a parquet file.
"""
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from src.config import BRANDS_YAML, SEASON_SHOWS, SYNTHETIC_RETAILER_YAML, TIERS, TRENDS_YAML


class ConfigError(Exception):
    """Raised when config/brands.yaml or config/trends.yaml is malformed."""


# --- brands.yaml ---


class Brand(BaseModel):
    name: str
    search_term: str


class _BrandOverride(BaseModel):
    name: str
    search_term: str


def _normalize_brand(entry: Union[str, dict]) -> Brand:
    if isinstance(entry, str):
        return Brand(name=entry, search_term=entry)
    override = _BrandOverride.model_validate(entry)
    return Brand(name=override.name, search_term=override.search_term)


class BrandsConfig(BaseModel):
    tiers: Dict[str, List[Brand]]

    @model_validator(mode="after")
    def _check_tiers(self) -> "BrandsConfig":
        expected = set(TIERS)
        actual = set(self.tiers.keys())
        if actual != expected:
            raise ValueError(
                f"brands.yaml tier keys {sorted(actual)} do not match expected {sorted(expected)}"
            )
        for tier, brands in self.tiers.items():
            if not brands:
                raise ValueError(f"brands.yaml tier '{tier}' has no brands")
        return self

    @classmethod
    def from_yaml(cls, path: Path = BRANDS_YAML) -> "BrandsConfig":
        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Could not read/parse {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} did not parse to a mapping of tier -> brands")
        try:
            tiers = {
                tier: [_normalize_brand(entry) for entry in entries]
                for tier, entries in raw.items()
            }
            return cls(tiers=tiers)
        except Exception as exc:
            raise ConfigError(f"{path} failed schema validation: {exc}") from exc


# --- trends.yaml ---


class TrendEvidence(BaseModel):
    brands: List[str]
    note: Optional[str] = None
    source: HttpUrl


class Trend(BaseModel):
    id: str
    label: str
    query_terms: List[str] = Field(min_length=1)
    context: Optional[List[str]] = None
    evidence: TrendEvidence
    # Which runway season the trend came from. The main pipeline (ingest,
    # analyze, Sage & Blush, agent) tracks FW26; SS27 trends exist only for
    # the forward watchlist, whose fetch window starts after the main one ends.
    season: str = "FW26"

    @field_validator("query_terms")
    @classmethod
    def _non_empty_terms(cls, v: List[str]) -> List[str]:
        if any(not t.strip() for t in v):
            raise ValueError("query_terms contains an empty/blank term")
        return v


class TrendsConfig(BaseModel):
    trends: List[Trend]

    @model_validator(mode="after")
    def _unique_ids(self) -> "TrendsConfig":
        ids = [t.id for t in self.trends]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"trends.yaml has duplicate trend ids: {sorted(dupes)}")
        if not self.trends:
            raise ValueError("trends.yaml has no trends")
        return self

    def for_season(self, season: Optional[str]) -> "TrendsConfig":
        if season is None:
            return self
        return TrendsConfig.model_construct(trends=[t for t in self.trends if t.season == season])

    @classmethod
    def from_yaml(cls, path: Path = TRENDS_YAML, season: Optional[str] = "FW26") -> "TrendsConfig":
        """The trends for one season (FW26 by default, so every existing caller
        keeps the dataset it was built on); season=None for all of them.
        The whole file is validated either way."""
        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Could not read/parse {path}: {exc}") from exc
        if not isinstance(raw, dict) or "trends" not in raw:
            raise ConfigError(f"{path} did not parse to a mapping with a top-level 'trends' key")
        try:
            cfg = cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"{path} failed schema validation: {exc}") from exc
        unknown = sorted({t.season for t in cfg.trends} - set(SEASON_SHOWS))
        if unknown:
            raise ConfigError(f"{path} has trends for unknown seasons {unknown}; add them to SEASON_SHOWS")
        if season is not None and season not in SEASON_SHOWS:
            raise ConfigError(f"unknown season {season!r}; expected one of {sorted(SEASON_SHOWS)}")
        return cfg.for_season(season)


# --- synthetic_retailer.yaml ---


class SyntheticDemand(BaseModel):
    beta: float = Field(gt=0)
    floor: float = Field(ge=0, lt=1)
    return_rate: float = Field(ge=0, lt=1)
    searches_per_unit: float = Field(gt=0)


class SyntheticInventory(BaseModel):
    replenish_every_weeks: int = Field(ge=1)
    par_weeks_of_cover: float = Field(gt=0)


class SyntheticProduct(BaseModel):
    sku_id: str
    product_name: str
    category: str
    attribute_type: str
    attribute: str
    trend_id: Optional[str]  # None = core product, the negative control
    price: float = Field(gt=0)
    base_weekly_units: float = Field(gt=0)
    search_term: str
    # Overrides the retailer-wide planted_lag_weeks for this product, so a
    # test can plant a different delay per product and demand each one back.
    planted_lag_weeks: Optional[int] = Field(default=None, ge=0, le=26)


class SyntheticRetailerConfig(BaseModel):
    retailer_name: str
    disclaimer: str
    seed: int
    driver_tier: str
    planted_lag_weeks: int = Field(ge=0, le=26)
    demand: SyntheticDemand
    inventory: SyntheticInventory
    size_curve: Dict[str, float]
    products: List[SyntheticProduct] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "SyntheticRetailerConfig":
        if self.driver_tier not in TIERS:
            raise ValueError(f"driver_tier '{self.driver_tier}' is not one of {TIERS}")
        total = sum(self.size_curve.values())
        if abs(total - 1.0) > 1e-9 or any(w <= 0 for w in self.size_curve.values()):
            raise ValueError(f"size_curve weights must be positive and sum to 1, got {total}")
        skus = [p.sku_id for p in self.products]
        dupes = {s for s in skus if skus.count(s) > 1}
        if dupes:
            raise ValueError(f"duplicate sku_ids: {sorted(dupes)}")
        if all(p.trend_id is not None for p in self.products):
            raise ValueError("no core (trend_id: null) products: the negative control is missing")
        return self

    def lag_for(self, product: SyntheticProduct) -> int:
        """The delay actually planted for this product."""
        return self.planted_lag_weeks if product.planted_lag_weeks is None else product.planted_lag_weeks

    @classmethod
    def from_yaml(
        cls, path: Path = SYNTHETIC_RETAILER_YAML, trends: Optional[TrendsConfig] = None
    ) -> "SyntheticRetailerConfig":
        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Could not read/parse {path}: {exc}") from exc
        try:
            cfg = cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"{path} failed schema validation: {exc}") from exc
        # Cross-file check: a typo'd trend_id would otherwise silently become a
        # product whose driver is all zeros.
        known = {t.id for t in (trends or TrendsConfig.from_yaml()).trends}
        unknown = sorted({p.trend_id for p in cfg.products if p.trend_id and p.trend_id not in known})
        if unknown:
            raise ConfigError(f"{path} references trend_ids not in trends.yaml: {unknown}")
        return cfg
