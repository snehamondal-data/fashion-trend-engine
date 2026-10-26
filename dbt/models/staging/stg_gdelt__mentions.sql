select trend_id, tier, date, value, norm, ingested_at
from {{ ref('stg_gdelt__timeline_points') }}
where trend_id != '_baseline'
