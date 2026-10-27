{{ config(materialized='table') }}

-- The one place real and synthetic data meet: real press momentum
-- (fct_weekly_mentions) vs Sage & Blush demand (synthetic). It lives in the
-- synthetic schema because anything derived from synthetic data is synthetic.
--
-- Grain: sku x candidate trend x tier x lag. corr_* is the Pearson
-- correlation between trend_share in week t and demand in week t + lag.
-- Nothing here knows the planted lag; mart_synthetic__recovered_lag checks
-- whether it falls out of the data.
--
-- Trend-tagged skus are tested against their own trend. Core skus are tested
-- against EVERY trend: they are the negative control, and testing them
-- 14 trends x 3 tiers x 13 lags times means some will correlate by chance.
-- That is the comparison to make, not "is the correlation non-zero".

with share as (

    -- Dense calendar: every baseline week for every trend. A week with no
    -- mentions is a real 0; a week whose tier baseline was 0 or too thin
    -- stays NULL (undefined) and corr() skips it. Same rule as the marts.
    select
        b.tier,
        b.week_start,
        t.trend_id,
        case
            when b.baseline_mention_count < {{ var('min_baseline_articles') }} then null
            else coalesce(m.trend_share, 0)
        end as trend_share
    from {{ ref('fct_weekly_baseline') }} as b
    cross join (select distinct trend_id from {{ ref('fct_weekly_mentions') }}) as t
    left join {{ ref('fct_weekly_mentions') }} as m
        on m.tier = b.tier and m.week_start = b.week_start and m.trend_id = t.trend_id

),

products as (

    select sku_id, trend_id from {{ ref('stg_synthetic__products') }}

),

pairs as (

    select sku_id, trend_id as candidate_trend_id, true as is_own_trend
    from products
    where trend_id is not null

    union all

    select p.sku_id, t.trend_id, false
    from products as p
    cross join (select distinct trend_id from share) as t
    where p.trend_id is null

),

lags as (

    select unnest(range(0, 13)) as lag_weeks

)

select
    pr.sku_id,
    pr.candidate_trend_id,
    pr.is_own_trend,
    s.tier,
    l.lag_weeks,
    -- corr() of a constant series (a trend with no mentions at all in a
    -- tier) is NaN, and DuckDB sorts NaN above every number -- one would
    -- win every max(). Undefined correlation is NULL.
    nullif(corr(s.trend_share, d.searches), 'NaN'::double) as corr_searches,
    nullif(corr(s.trend_share, d.units_sold), 'NaN'::double) as corr_units_sold,
    count(s.trend_share) as n_weeks
from pairs as pr
cross join lags as l
inner join share as s
    on s.trend_id = pr.candidate_trend_id
inner join {{ ref('fct_synthetic__sku_week') }} as d
    on d.sku_id = pr.sku_id
    and d.week_start = s.week_start + to_days(7 * l.lag_weeks)
group by all
