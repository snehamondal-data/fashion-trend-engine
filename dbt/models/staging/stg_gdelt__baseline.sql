-- The brands-only query per tier: the denominator for every trend_share.
select tier, date, value, norm, ingested_at
from {{ ref('stg_gdelt__timeline_points') }}
where trend_id = '_baseline'
