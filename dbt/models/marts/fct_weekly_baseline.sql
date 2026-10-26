-- date_trunc('week') is Monday-start, matching transform.py's ISO weeks.
select
    tier,
    cast(date_trunc('week', date) as timestamp) as week_start,
    sum(value) as baseline_mention_count
from {{ ref('stg_gdelt__baseline') }}
group by 1, 2
