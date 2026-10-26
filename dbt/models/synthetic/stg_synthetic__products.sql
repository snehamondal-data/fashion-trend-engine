-- SYNTHETIC: Sage & Blush is a fictional retailer (config/synthetic_retailer.yaml).
select
    sku_id,
    retailer,
    product_name,
    category,
    attribute_type,
    attribute,
    trend_id,
    cast(price as decimal(10, 2)) as price,
    base_weekly_units,
    search_term
from read_parquet('{{ var("synthetic_dir") }}/products.parquet')
