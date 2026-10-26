-- transform.py raised on a malformed cache filename; this is the same check.
-- Without it a bad name would silently yield NULL trend_id/tier rows.
select distinct source_file
from {{ ref('stg_gdelt__timeline_points') }}
where len(string_split(regexp_replace(source_file, '^.*/|\.json$', '', 'g'), '__')) != 3
