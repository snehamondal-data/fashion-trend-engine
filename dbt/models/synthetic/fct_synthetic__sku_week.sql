{{ config(materialized='table') }}

-- SYNTHETIC. One row per sku x week: sizes rolled up, searches joined,
-- revenue and stockout flags derived. The table merchandising would read.

with inventory as (

    select
        week_start,
        sku_id,
        sum(on_hand_start) as on_hand_start,
        sum(units_received) as units_received,
        sum(units_sold) as units_sold,
        sum(units_returned) as units_returned,
        sum(on_hand_end) as on_hand_end,
        count(*) filter (where on_hand_end = 0) as sizes_sold_out,
        -- S and M carry over half of demand (size_curve), so they sell out
        -- first and cost the most when they do.
        bool_or(on_hand_end = 0 and size in ('S', 'M')) as core_size_sold_out
    from {{ ref('stg_synthetic__inventory_weekly') }}
    group by 1, 2

),

searches as (

    select week_start, sku_id, sum(searches) as searches
    from {{ ref('stg_synthetic__site_searches') }}
    group by 1, 2

)

select
    i.week_start,
    i.sku_id,
    p.trend_id,
    p.category,
    p.attribute,
    i.on_hand_start,
    i.units_received,
    i.units_sold,
    i.units_returned,
    i.on_hand_end,
    coalesce(s.searches, 0) as searches,
    i.units_sold * p.price as gross_revenue,
    (i.units_sold - i.units_returned) * p.price as net_revenue,
    i.units_sold / nullif(i.on_hand_start + i.units_received, 0) as sell_through,
    i.sizes_sold_out,
    i.core_size_sold_out
from inventory as i
inner join {{ ref('stg_synthetic__products') }} as p using (sku_id)
left join searches as s using (week_start, sku_id)
