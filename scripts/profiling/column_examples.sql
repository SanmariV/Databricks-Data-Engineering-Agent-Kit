-- Most frequent values of one dimension column, from a fixed-size sample.
-- Feeds the "Data Example" column. Never run for columns tagged PII; the
-- script skips them before this template is rendered.
-- Parameters: $relation, $column, $sample_rows, $max_examples
with

sampled as (

    select $column as value
    from $relation tablesample ($sample_rows rows) repeatable (42)
    where $column is not null

),

counted as (

    select
        cast(value as string) as example_value,
        count(*) as row_count
    from sampled
    group by 1

)

select example_value
from counted
order by row_count desc, example_value
limit $max_examples
