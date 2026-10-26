-- Gap weeks (no mentions at all) stay absent rather than zero-filled here;
-- analyze.py densifies the calendar itself, where 0 and undefined differ.

with weekly as (

    select
        trend_id,
        tier,
        cast(date_trunc('week', date) as timestamp) as week_start,
        sum(value) as mention_count
    from {{ ref('stg_gdelt__mentions') }}
    group by 1, 2, 3

)

select
    w.trend_id,
    w.tier,
    w.week_start,
    w.mention_count,
    b.baseline_mention_count,
    -- A missing or zero baseline is an undefined share: NULL, never inf.
    w.mention_count / nullif(b.baseline_mention_count, 0) as trend_share
from weekly as w
left join {{ ref('fct_weekly_baseline') }} as b
    using (tier, week_start)
