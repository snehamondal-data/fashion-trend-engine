-- SYNTHETIC. Grain: sku x search term x week. Unlike sales, searches keep
-- counting when an item is sold out -- the uncensored demand signal.
select
    cast(week_start as timestamp) as week_start,
    sku_id,
    search_term,
    searches
from read_parquet('{{ var("synthetic_dir") }}/site_searches.parquet')
