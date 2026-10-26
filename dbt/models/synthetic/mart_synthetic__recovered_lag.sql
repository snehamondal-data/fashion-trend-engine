{{ config(materialized='table') }}

-- Per trend-tagged sku and tier: the lag at which press momentum best
-- predicts demand, measured on searches (uncensored) and on units sold
-- (capped by stock). If the method works, the driver tier's best lag on
-- searches equals planted_lag_weeks in config/synthetic_retailer.yaml.
--
-- noise_ceiling: the strongest correlation any core sku reached against any
-- trend, tier or lag -- what pure chance produces in this many tries. A
-- recovered lag only counts if it clears that.

with noise as (

    select max(corr_searches) as noise_ceiling
    from {{ ref('mart_synthetic__lag_correlation') }}
    where not is_own_trend

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
    noise.noise_ceiling,
    own.best_corr_searches > noise.noise_ceiling as clears_noise_ceiling
from own
cross join noise
