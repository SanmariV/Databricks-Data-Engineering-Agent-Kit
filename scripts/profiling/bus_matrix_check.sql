-- BUS Matrix check: does every key on the fact find its dimension row?
-- One query per relationships test from a fct_ model to a dim_ model.
-- dim_duplicate_keys = 0 and fact_rows_no_match = 0 means the join is safe.
-- Parameters: $fact_relation, $fact_key, $dim_relation, $dim_key,
-- $sample_limit (0 when the key column is tagged PII)
with

dim as (

    select $dim_key as dim_key from $dim_relation

),

fact as (

    select $fact_key as fact_key from $fact_relation

),

dim_stats as (

    select
        count(*) as dim_rows,
        count(*) - count(distinct dim_key) as dim_duplicate_keys
    from dim

),

fact_stats as (

    select
        count(*) as fact_rows,
        count_if(fact_key is null) as fact_rows_null_key,
        count(distinct fact_key) as fact_distinct_keys
    from fact

),

unmatched as (

    select fact.fact_key
    from fact
    left join (select distinct dim_key from dim) as dim_keys
        on fact.fact_key = dim_keys.dim_key
    where
        fact.fact_key is not null
        and dim_keys.dim_key is null

),

unmatched_stats as (

    select
        count(*) as fact_rows_no_match,
        count(distinct fact_key) as distinct_keys_no_match
    from unmatched

),

unmatched_sample as (

    select distinct cast(fact_key as string) as fact_key
    from unmatched
    order by fact_key
    limit $sample_limit

),

sample_list as (

    select
        array_join(array_sort(collect_list(fact_key)), ', ')
            as sample_keys_no_match
    from unmatched_sample

)

select *
from dim_stats
cross join fact_stats
cross join unmatched_stats
cross join sample_list
