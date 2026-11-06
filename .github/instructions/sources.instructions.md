---
applyTo: "models/sources/**"
---

# Bronze sources

One file per source system: `models/sources/<source>.yml`. Bronze is read-only; ingestion pipelines load it into the bronze catalog.

- Start the file with `python -m dbde generate-source <source> --target dev`. Add `--schema <name>` when the bronze schema differs from the source name. Rerunning it keeps your descriptions and adds new tables and columns.
- Fill in the source `description` (what the system is, how it lands) and each table's `description` (what one row is). Replace every `TODO(...)` you can answer, and list the rest.
- `freshness` needs `loaded_at_column` (usually `_ingested_at`), `warn_after_hours` and `error_after_hours`. If a table has no load timestamp, say so. Don't invent one.
- Column `data_type` values are what Unity Catalog reports. Don't edit them; they are documentation.
- Every bronze table gets exactly one silver source table (`models/silver/<source>/<Entity>.yml`).
