{{ config(materialized='table') }}

-- Per trend-tagged sku and tier: the lag at which press momentum best
-- predicts demand, measured on searches (uncensored) and on units sold
-- (capped by stock). If the method works, the driver tier's best lag on
-- searches equals the planted lag in config/synthetic_retailer.yaml.
--
-- noise_ceiling: the strongest correlation chance produced for THIS sku --
-- its demand against the 13 other trends' press (any tier, any lag) -- or
-- any core sku reached against any trend. A recovered lag only counts if
-- its own trend beats every decoy.
--
-- Why per sku, not just the core skus: a trend sku's demand is spiky like
-- press is, and two unrelated spiky series correlate by chance far more
-- often than spiky press and flat core demand do. A core-only ceiling was
-- too low: the null test (tests/test_synthetic_retailer.py) caught it.

with core as (

    select max(lc.corr_searches) as core_ceiling
    from {{ ref('mart_synthetic__lag_correlation') }} as lc
    inner join {{ ref('stg_synthetic__products') }} as p using (sku_id)
    where p.trend_id is null

),

decoys as (

    select sku_id, max(corr_searches) as decoy_ceiling
    from {{ ref('mart_synthetic__lag_correlation') }}
    where not is_own_trend
    group by 1

),

own as (

    select
        sku_id,
        candidate_trend_id as trend_id,
        tier,
        arg_max(lag_weeks, corr_searches) as best_lag_searches,
        max(corr_searches) as best_corr_searches,
        arg_max(lag_weeks, corr_units_sold) as best_lag_units_sold,
        max(corr_units_sold) as best_corr_units_sold,
        min(n_weeks) as min_n_weeks
    from {{ ref('mart_synthetic__lag_correlation') }}
    where is_own_trend
    group by 1, 2, 3

)

select
    own.*,
    greatest(decoys.decoy_ceiling, core.core_ceiling) as noise_ceiling,
    own.best_corr_searches > greatest(decoys.decoy_ceiling, core.core_ceiling) as clears_noise_ceiling
from own
inner join decoys using (sku_id)
cross join core
