-- The orphaned-cache guard. Raw filenames embed a hash of the query, so any
-- edit to brands.yaml or query_builder.py leaves the old file behind next to
-- the new one -- and both get summed. That already happened once: an
-- unquoted-Hermès luxury baseline doubled every luxury denominator and
-- halved every luxury trend_share. Fails listing the colliding files.
select
    trend_id,
    tier,
    count(distinct source_file) as file_count,
    string_agg(distinct source_file, ', ') as files
from {{ ref('stg_gdelt__timeline_points') }}
group by 1, 2
having count(distinct source_file) > 1
