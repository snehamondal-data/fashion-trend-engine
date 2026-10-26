{#- dbt's default would name the schema `main_synthetic` (target + custom).
    Use the custom name as-is so the boundary reads plainly: main vs synthetic. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name or target.schema) | trim }}
{%- endmacro %}
