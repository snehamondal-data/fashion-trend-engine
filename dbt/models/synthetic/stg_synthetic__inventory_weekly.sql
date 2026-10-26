-- SYNTHETIC. Grain: sku x size x week. units_sold is capped by stock on
-- hand, so in a sold-out week it understates demand.
select
    cast(week_start as timestamp) as week_start,
    sku_id,
    size,
    on_hand_start,
    units_received,
    units_sold,
    units_returned,
    on_hand_end
from read_parquet('{{ var("synthetic_dir") }}/inventory_weekly.parquet')
