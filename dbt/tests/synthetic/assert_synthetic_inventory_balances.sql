{{ config(tags=['synthetic']) }}

-- Stock conservation, per sku x size: each week's books balance, nothing goes
-- negative, and this week's opening stock is last week's closing stock.
-- A generator bug that creates or loses units shows up here.

with ledger as (

    select
        *,
        lag(on_hand_end) over (partition by sku_id, size order by week_start) as prev_on_hand_end
    from {{ ref('stg_synthetic__inventory_weekly') }}

)

select *
from ledger
where on_hand_end != on_hand_start + units_received - units_sold
   or on_hand_end < 0
   or units_sold < 0
   or units_returned > units_sold
   or (prev_on_hand_end is not null and on_hand_start != prev_on_hand_end)
