---
agent: agent
description: Scaffold a Databricks data engineering project (no dbt) to the standards, with one silver source table per bronze table for the given source systems.
---

# Scaffold a Databricks project

Set up the project **${input:project:project name, snake_case, e.g. colina_analytics}**.

- Bronze source systems (schemas in the bronze catalog): **${input:sources:comma separated, e.g. policy_admin, crm}**
- Gold domains (one folder and gold schema each): **${input:domains:comma separated, e.g. sales, distribution}**
- Requirements Information Matrix (optional): `${input:matrix:path to an .xlsx, or none}`
- Column data dictionary (optional): `${input:dictionary:full path to the .xlsx or .csv that describes the columns, or none}`

If the dictionary input is empty, ask once: "Do you have a file that describes the columns (a data dictionary)? Paste its full path, or say none." Wait for the answer before step 3.

Follow `AGENTS.md`, `.github/instructions/` and `databricks_engineering_standards.md`.

## Rules for this task

- NEVER invent environment values: workspace hosts, SQL warehouse names, catalog names, service principals, group names, the repo URL. Write `TODO(<what>)` and list each one in your report.
- NEVER guess a business key. If a bronze table has no obvious key, check the data (one counts-only query), then ask, or leave `business_key` out and list the table under "Needs a decision".
- Read-only against Databricks, with `--target dev` and your own CLI profile. Never target test or prod.
- Don't write gold specs or integrated silver tables. Business logic needs a person. This task creates the structure, the silver source tables and, optionally, gold specifications in `docs/specs/`.
- Never edit `databricks.yml`, `resources/`, `notebooks/`, `gold/` or `genie/` by hand; `python -m dbde render` writes them.

## 0. Preflight (before creating anything)

1. `pip install -r requirements.txt` if `python -m dbde --help` fails.
2. `python -m dbde debug --target dev`.
   - If it says `set targets.dev.warehouse`, or sign-in fails, the workspace is not connected yet. Do steps 1 to 4 of `.github/prompts/connect-workspace.prompt.md` now. Ask the user in chat for the workspace URL, the SQL warehouse names and the catalog names. Use the project name from this prompt, write `dbde.yml`, sign in, then run `debug` again. Don't tell the user to edit files themselves.
   - If it lists missing catalogs: stop and tell the user to create them, or ask an admin (`START_HERE.md`, step 3). Or ask them for the real catalog names and write them to `dbde.yml`.
3. If the repo path is longer than about 100 characters, warn once: "Move the repo to a short path such as `C:\dev\...`." Continue regardless.
4. If a secret (token, password, client secret) appears in any file you open, do not copy or echo it. Say which file has it and tell the user to rotate it.

## Rules for decisions and failures

- Never stop to ask the user to choose between options that need admin rights. Pick the safest working default, apply it, and record it under "Decisions made for you".
- Every item under "Needs a decision" comes with your recommended answer, e.g. "`policy_status`: dictionary says IF/LA, data also has `RE`. Recommend: accept RE and ask what it means."

## 1. Project settings

- In `dbde.yml` set `project:` to the project name. Keep the default catalog names (`<env>_bronze`, ...) unless the user's metastore uses different ones; then set `targets.<env>.catalogs`.
- `reference/business_glossary.csv`: the header row only (`category,concept,acronym,context,description`). Remove example rows from another project.
- Create `models/gold/<domain>/` for each domain, each with a `.gitkeep`.

## 2. Sources (one file per bronze schema)

For each source system:

```bash
python -m dbde generate-source <source> --target dev
```

Then edit `models/sources/<source>.yml`:

- Add a `description` for the source and for each table (what one row is).
- Keep the `freshness` rule. If a table has no `_ingested_at`, set `loaded_at_column` to the real load timestamp, or say so and list it. Don't make one up.

## 3. Silver source tables (one per bronze table)

For each bronze table, write `models/silver/<source>/<Entity>.yml` following `.github/instructions/silver.instructions.md`:

1. Name the table: PascalCase, singular, business terms (`policy` → `Policy`, `agt_mstr` → `Agent`).
2. One column per useful bronze column, with an `expression` over the bronze column:
   - business key `<Entity>_BK` with `business_key: true`
   - other keys keep their entity's `_BK` name
   - `Is`/`Has` booleans, `...Date` dates, `...Timestamp` timestamps, `...Amount` `decimal(18,2)`
   - codes `upper(trim(...))`
   - no abbreviations
   Order the columns: keys, strings, numerics, booleans, dates, timestamps.
3. `load`:
   - `{mode: incremental, column: _ingested_at, lookback_days: 3}` for tables landed incrementally
   - `{mode: full}` for small or fully reloaded tables
   Add `dedupe_order_by: ["_ingested_at desc"]` when bronze can hold several versions of a key. Check with one counts-only query: `count(*)` vs `count(distinct <key>)`.
4. A description that states the grain, and a description on every column.
5. `accepted_values` only where the complete set of values is known (dictionary or confirmed by the user).
6. `meta.pii: true` on personal data (see `AGENTS.md`).
7. Where you can't tell what a column means: `description: TODO(meaning of <bronze column>)`, and list it.

### Column descriptions from a data dictionary (only if a dictionary path was given)

Load the dictionary read-only before writing any spec:

```bash
python -I scripts/read_data_dictionary.py "<dictionary path>" [--sheet "<name>"]
```

It prints JSON keyed by lower-cased bronze column name, with `description` and `expanded`. Never edit or save the file.

- Match each **bronze** column name to a dictionary entry, case-insensitively.
- Column `description` = the dictionary `description`, plus `expanded` when it adds meaning (allowed values, units, format). Keep the wording faithful.
- Use the dictionary to settle meanings and to propose the grain and business key. If it doesn't say, still never guess a key.
- Add `accepted_values` only if the dictionary lists the complete set.
- A dictionary entry with no matching bronze column: don't create a column; list it under "Needs a decision".

## 4. Gold specifications from the Information Matrix (only if a matrix path was given)

Read the matrix **read-only** with a short openpyxl script (never save it). From EntitiesDimensions, Measure Groups, Attributes, Measures and BUS Matrix-current:

- Plan `Dim<Entity>` per entity and `Fact<Process>` per measure group. Put the matrix name in `meta.matrix.name`.
- Write the plans to `docs/specs/<domain>/<Table>.yml`, **not** to `models/`, until the SELECT exists. Each plan has:
  - description and grain
  - columns with Databricks types and descriptions
  - `meta.matrix` copied from the matrix
  - `references` for each 1:M cell in the BUS Matrix
  - `measures` with descriptions
- Copy values exactly; leave blank cells blank. Note which silver columns each attribute comes from. That shows which integrated tables are needed.

## 5. Render and verify

Run these, and fix any failure before you report:

```bash
python -m dbde render
python -m dbde check
python -m dbde build --target dev --layer silver
```

`build` deploys the bundle (notebooks, shared library, jobs), runs every silver notebook through the `dbde_daily_load` job and runs the source and silver tests.

- If the bundle deploy fails on permissions or a missing warehouse: tell the user to do step 5 of `START_HERE.md`.
- If source freshness fails only because dev bronze is old, rerun with `--skip-source-tests` and say so.

## 6. Report back

Keep the chat short. Write the full report to `docs/scaffold_report.md` and print only the summary below.

### Report file (`docs/scaffold_report.md`)

- files created, grouped by step
- every `TODO(...)` left, with file, in three lists:
  1. **Blocking**: needed before the build works (sign-in, warehouse, catalogs, permissions)
  2. **Data questions**: meanings, units, allowed values, keys; one line each, answerable in one reply
  3. **Deployment placeholders**: release pipeline, grants, service principals; can be ignored for a dev test
- **Needs a decision**: tables without a clear key, unknown columns, missing load timestamps, dictionary entries without a bronze column
- dictionary coverage: columns described from the dictionary vs still `TODO`
- PII columns marked
- the result of each command in step 5
- next step: `/build-intermediate` for one domain, then `/build-marts`, then `/build-genie-space`

### Chat summary (the only thing printed in chat)

```
Scaffold finished. Full report: docs/scaffold_report.md

Result: <n> sources, <n> silver tables, <n> tests (<n> pass), <n> column descriptions from the dictionary, <n> TODOs.
Checks: dbde check <pass/fail> | dbde render <up to date> | dbde build silver <pass/fail/not run>

Your next steps:
1. <the single most important action, with the exact file or command>
2. ...
(at most 5 steps, in order; the first step is a blocking item if any exist)

Reply "go" and I will <what the agent will do next>, or tell me which step you want help with.
```
