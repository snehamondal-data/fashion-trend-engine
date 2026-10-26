{#- Grain check: no two rows share these columns. Written here instead of
    pulling in dbt_utils for a single macro. -#}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
