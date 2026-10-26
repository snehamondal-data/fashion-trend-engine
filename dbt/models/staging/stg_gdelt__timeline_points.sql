-- One row per (raw file, timeline point). The column schema is declared
-- explicitly rather than inferred: a file whose shape drifts fails here,
-- loudly, instead of being coerced (same contract as CachedResponse).
-- source_file is kept for lineage -- it is what makes an orphaned cache file
-- traceable (see tests/assert_one_raw_file_per_trend_tier.sql).

with raw as (

    select filename, meta, response
    from read_json(
        '{{ var("raw_glob") }}',
        filename = true,
        format = 'auto',
        columns = {
            meta: 'STRUCT(query VARCHAR, timespan VARCHAR, url VARCHAR, fetched_at VARCHAR)',
            response: 'STRUCT(timeline STRUCT(series VARCHAR, data STRUCT(date VARCHAR, value DOUBLE, norm DOUBLE)[])[])'
        }
    )

),

files as (

    select
        filename as source_file,
        -- {trend_id}__{tier}__{hash}.json; shape is asserted in
        -- tests/assert_raw_filenames_well_formed.sql
        string_split(regexp_replace(filename, '^.*/|\.json$', '', 'g'), '__') as name_parts,
        cast(meta.fetched_at as timestamptz) as ingested_at,
        response.timeline as timeline
    from raw

),

series as (

    select source_file, name_parts, ingested_at, unnest(timeline) as s
    from files

),

points as (

    select source_file, name_parts, ingested_at, unnest(s.data) as p
    from series

)

select
    name_parts[1] as trend_id,
    name_parts[2] as tier,
    strptime(p.date, '%Y%m%dT%H%M%SZ') as date,
    p.value as value,
    p.norm as norm,
    ingested_at,
    source_file
from points
