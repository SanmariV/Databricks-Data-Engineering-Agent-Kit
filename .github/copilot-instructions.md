# Copilot instructions

This is a Databricks data engineering project **without dbt**. You write table specs (`models/**/*.yml`), SELECTs (`models/**/*.sql`) and Genie space specs (`models/genie/*.yml`). The `dbde` toolkit (`python -m dbde`) renders and deploys the code, then runs and tests it:

- silver: PySpark notebooks (`notebooks/silver/`) with Delta MERGE
- gold: Unity Catalog tables, SQL stored procedures and metric views (`gold/`)
- Genie spaces (`genie/`, `resources/genie_*.yml`) and the Asset Bundle (`databricks.yml`, `resources/`)

Calybre DE Standards Policy v2 applies in full.

Full rules: `AGENTS.md` (root) and `databricks_engineering_standards.md`. Folder-specific rules are in `.github/instructions/`.

## Layer flow

bronze (`models/sources/`) → silver source tables (`models/silver/<source>/`, spec only) → silver integrated tables (`models/silver/<domain>/`, Spark SQL) → gold `Dim`/`Fact`/`Bridge` (`models/gold/<domain>/`, Databricks SQL) → metric views → Genie spaces (`models/genie/`)

- Never edit `databricks.yml`, `resources/**`, `notebooks/**`, `gold/**` or `genie/**`. Change the spec or SELECT and run `python -m dbde render`.
- Read tables only with `{{ ref('schema.Table') }}`. Never hard-code a catalog name.
- Silver never reads gold. Silver source tables never join or aggregate.
- Never write notebooks, stored procedures, MERGE, DDL, metric views, Genie JSON or GRANT by hand.

## AI enablement

- Metric views (`Metric<Process>`) are rendered from fact `measures:`; Genie spaces from `models/genie/<Name>.yml`. Never edit them by hand.
- Describe every column and measure. Measures are aggregates. `synonyms` and `ai_instructions` are only written when a person gave them.
- PII never reaches Genie: no table with PII columns in a space, no PII column names in instructions or examples. See `.github/instructions/genie.instructions.md`.

## Naming

- Tables are PascalCase and singular. Gold tables start with `Dim`, `Fact` or `Bridge`.
- Columns are PascalCase:
  - business keys `<Entity>_BK` (with `business_key: true`)
  - booleans `Is`/`Has`
  - `...Date` is a date, `...Timestamp` is a UTC timestamp
  - money `...Amount`, `decimal(18,2)`
  - no abbreviations
- The kit adds `_SK`, `StartDate`/`EndDate`/`IsCurrent` and the audit columns. Never declare them.

## Every table

- The description states `Grain = ...`. Every column is described (meaning, unit, allowed values).
- Business tests (`accepted_values`, `accepted_range`, `expression`, `reconciles_to`). Key tests are implied.
- PII columns carry `meta.pii: true` in every layer. Never print PII values.
- No `current_date()`, `current_timestamp()`, `rand()` or `uuid()` in logic. Dates come from `load_date` / `RunLoadDate`.

## Before you finish

Run `python -m dbde render`; `python -m dbde check` must pass. End with the "How to finish every task" block in `AGENTS.md`.

## Code review

Flag issues in this order:

1. Hand edits to rendered files
2. Layer violations
3. Missing keys, tests or descriptions
4. PII reaching Genie
5. Naming and type violations
6. Non-deterministic logic
7. Style

Cite the rule id (`databricks_engineering_standards.md`, section 12).
