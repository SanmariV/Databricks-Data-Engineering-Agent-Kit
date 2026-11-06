---
applyTo: "models/silver/**"
---

# Silver (PySpark notebooks, Delta)

The folder is the schema; the file name is the table. Each silver table is rendered into one notebook, `notebooks/silver/nb_silver_<schema>_<table>.py`. The notebook reads, conforms and MERGEs with the shared library `src/databricks_de_common`.

## Source tables: `models/silver/<source>/<Entity>.yml` (no `.sql`)

There is one per bronze table. A source table may:

- rename and cast
- trim and upper-case codes
- replace null codes with `'UNKNOWN'`
- set `Is`/`Has` flags and simple `case` buckets
- convert timestamps to UTC

It never joins, aggregates or applies a business rule.

```yaml
description: >
  One row per policy as currently held in the policy admin system.
  Grain = Policy_BK.
source: {source: policy_admin, table: policy}
load: {mode: incremental, column: _ingested_at, lookback_days: 3}
dedupe_order_by: ["_ingested_at desc"]
columns:
  - name: Policy_BK
    data_type: string
    expression: upper(trim(policy_no))
    business_key: true
    description: Policy number from the policy admin system.
```

- Every column has an `expression` in Spark SQL over the bronze columns.
- `dedupe_order_by` is needed when bronze holds several versions of a key. Without it, a repeated key fails the run.
- `load.mode: incremental` needs `load.column` (the bronze load timestamp) and `lookback_days`.

## Integrated tables: `models/silver/<domain>/<Table>.yml` + `<Table>.sql`

Joins, business rules and re-graining. The SELECT is Spark SQL:

- read silver tables only, through `{{ ref('schema.Table') }}`
- return exactly the spec's columns, one row per business key
- `:load_date` may be used for date windows
- no `current_date()`, `current_timestamp()`, `rand()` or `uuid()`

Keep each table to about 4 to 6 inputs, and one business rule per table where you can, so each rule can be tested.

## Types

`string`, `varchar(n)`, `boolean`, `int`, `bigint`, `decimal(18,2)` for money, `decimal(18,8)` for rates, `date`, `timestamp` (UTC). Never a floating type for money.

## Never

- Never declare the audit columns; the library adds them.
- Never edit `notebooks/**`. Change the spec and run `python -m dbde render`.
- Never use `mergeSchema` or schema inference. The notebook adds new spec columns with `ALTER TABLE ADD COLUMNS`. Renames and type changes need a migration notebook, run once and recorded in the pull request.
