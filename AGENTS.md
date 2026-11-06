# AGENTS.md

Instructions for GitHub Copilot (Chat, agent mode, code review and the coding agent), Claude Code and other coding agents working in this repository.
The full rationale lives in `databricks_engineering_standards.md`. These rules win over your general habits. If a request conflicts with a rule, say so instead of breaking it silently.

## What this repo is

A Databricks data engineering project **without dbt**. You write table **specs** (YAML), **SELECTs** and **Genie space specs**. The `dbde` toolkit renders the code Databricks runs, deploys it, runs it and tests it.

- Silver: Delta tables in the silver catalog, loaded by one PySpark notebook per table with Delta MERGE.
- Gold: Delta tables in the gold catalog, loaded by one Unity Catalog SQL stored procedure per table with MERGE. Facts with measures get a metric view.
- Genie: spaces over gold tables and metric views, rendered from `models/genie/<Name>.yml`.
- Bronze is loaded by ingestion pipelines, not by this repo. This repo only reads it.
- Calybre DE Standards Policy v2 applies in full: `_BK` / `_SK` keys, the four audit columns, `StartDate` / `EndDate` / `IsCurrent`, notebooks in silver, stored procedures in gold, OPTIMIZE / VACUUM.

```
bronze (models/sources) → silver source tables → silver integrated tables → gold Dim/Fact/Bridge → metric views → Genie spaces
```

## What you edit, and what you never edit

| You write | Never edit by hand (rendered by `python -m dbde render`) |
| --- | --- |
| `models/sources/<source>.yml` (start it with `dbde generate-source`) | `databricks.yml`, `resources/*.yml` |
| `models/silver/<schema>/<Table>.yml` (+ `.sql` for integrated tables) | `notebooks/**` |
| `models/gold/<domain>/<Table>.yml` + `.sql` | `gold/**` (tables, procedures, metric views, ops, grants) |
| `models/genie/<Name>.yml` (Genie spaces) | `genie/*.geniespace.json` |
| `reference/business_glossary.csv`, `dbde.yml` | |
| `src/databricks_de_common/**` and `dbde/**` only for a toolkit change, with tests | |

After any change under `models/` or to `dbde.yml`, run `python -m dbde render` and commit the rendered files with the spec. `python -m dbde check` fails on hand edits and stale files (rule R1).

Do not write notebooks, stored procedures, `MERGE`, `CREATE TABLE`, metric views, Genie JSON, job YAML or `GRANT` statements yourself. Express the change in the spec or the SELECT. If something cannot be expressed that way, stop and say so. Do not work around it.

## Layer rules (never break these)

| Layer | Folder | Reads | Built as |
| --- | --- | --- | --- |
| Silver source table | `models/silver/<source>/` | exactly one bronze table, named in the spec's `source:` | Spec only: an `expression` per column (Spark SQL over bronze columns) |
| Silver integrated table | `models/silver/<domain>/` | silver tables via `{{ ref('schema.Table') }}` | Spec + Spark SQL SELECT |
| Gold table | `models/gold/<domain>/` | silver and gold tables via `{{ ref() }}` | Spec + Databricks SQL SELECT |
| Genie space | `models/genie/` | gold tables and metric views | Spec with examples over `{{ ref() }}` |

- One silver source table per bronze table (1:1). Source tables rename, cast, trim, upper-case codes, handle nulls and set `Is`/`Has` flags. They never join, aggregate or apply business rules.
- Joins, business rules and re-graining go in integrated silver tables (the "intermediate" layer) or in the gold SELECT.
- Silver never reads gold. A dimension reading a fact is a design smell. Gold tables never read metric views.
- Never hard-code catalog names (`dev_silver.`, `prod_gold.`, ...). Use `{{ ref('schema.Table') }}`. The kit resolves it per environment.
- The schema of a table is its folder. The table name is its file name. The schema `ops` is reserved.
- Exception: when `dbde.yml` sets `layer_schemas` (one catalog with `bronze` / `silver` / `gold` schemas), every silver table goes into the `silver` schema and every gold table into `gold`. Folders still group the specs and `ref()` keys. Table names must be unique per layer. If a workspace has one catalog with layer schemas and `layer_schemas` is missing, set it. Don't let the kit create folder schemas there.

## Specs

```yaml
# models/gold/distribution/DimAgent.yml
description: One row per agent version (SCD type 2). Grain = Agent_BK, StartDate.
scd_type: 2                      # dimensions only: 1 or 2
columns:
  - name: Agent_BK
    data_type: varchar(20)       # Databricks type
    business_key: true
    description: Agent code from the policy admin system.
  - name: AgentName
    data_type: varchar(200)
    description: Agent full name.
    meta: {pii: true, matrix: {example: "Thabo M***"}}
```

- Every table: a description with `Grain = ...`, at least one `business_key: true` column, and a description on every column (meaning, unit, allowed values). Descriptions become Unity Catalog comments, which Genie reads.
- Never declare the columns the kit adds: `<Table>_SK`, `StartDate`, `EndDate`, `IsCurrent` and the four audit columns. Never name a column after a procedure variable (`RunLoadDate`, `RunLoadStepID`, `RunTimestamp`, `MaxKey`, `NewKey`, `MergeKey<n>`).
- Facts point to dimensions with `<Role><DimTable>_SK bigint` + `references: <domain>.<DimTable>`. The SELECT resolves the key with a point-in-time join and `coalesce(..., -1)`.
- Large facts: `load: {mode: incremental, column: <date column>, lookback_days: <n>}`.
- Measures go on facts (`measures:` with `name`, `expression` (an aggregate), `description`). They become the fact's metric view, `Metric<Process>`.

## Naming

| Thing | Pattern | Example |
| --- | --- | --- |
| Schema (folder) | snake_case: source name, or domain | `policy_admin`, `sales` |
| Silver table | PascalCase singular, no prefix | `Policy`, `NewBusinessPolicy` |
| Gold table | `Dim` / `Fact` / `Bridge` + PascalCase | `DimAgent`, `FactNewBusiness` |
| Metric view (rendered) | `Metric` + process | `MetricNewBusiness` |
| Business key | `<Entity>_BK` | `Policy_BK` |
| Boolean | `Is` / `Has`, `boolean` | `IsActive` |
| Date / timestamp | `...Date` (date), `...Timestamp` (timestamp, UTC) | `IssueDate` |
| Money | `...Amount`, `decimal(18,2)` | `AnnualPremiumAmount` |
| Code | `...Code`, upper-cased | `PolicyStatusCode` |
| Genie space | PascalCase file name | `SalesAnalyst` |

No abbreviations (`Amount`, not `Amt`; `Number`, not `No`). Types: `string`, `varchar(n)`, `decimal(p,s)`, `boolean`, `date`, `timestamp`, `int`, `bigint`. Never T-SQL types such as `nvarchar`, `datetime`, `money` or `bit` (rule D2).

## SQL style (the `.sql` files)

- Lowercase keywords, 4-space indents, trailing commas, lines of 80 characters or fewer.
- Import CTEs first (one per `ref`), then one-job CTEs, then a final `select`.
- Explicit `as`. `inner join` / `left join` only. No single-letter aliases. Prefix columns with the CTE name when joining.
- Gold SELECTs run inside the load procedure: no `:parameters`, no `${...}`, no `limit`, no `order by`, no `group by all`.
- Silver integrated SELECTs are Spark SQL and MAY use `:load_date`.
- Never use `current_date()`, `current_timestamp()`, `now()`, `rand()` or `uuid()` in logic. Dates come from `load_date` / `RunLoadDate`.

## Tests

The kit adds key, uniqueness, SCD2 and relationship tests to every table (`databricks_engineering_standards.md`, section 8.1). You add the business ones:

- `accepted_values` on every code column whose allowed values are known
- `accepted_range` on amounts, ages and rates
- `expression` for row rules (`IssueDate >= ApplicationDate`)
- `reconciles_to` a control total on every financial fact
- `row_count_between` / `recency` on key facts

Never delete or weaken a failing test to make a build pass. A failing test is a data quality finding: report it.

## Commands

```bash
python -m dbde parse                    # specs load; writes target/manifest.json
python -m dbde check                    # all rules + render drift (run before every commit)
python -m dbde render                   # regenerate notebooks, SQL, metric views, Genie, bundle
python -m dbde ls --tests               # list tables, metric views, spaces and tests
python -m dbde debug --target dev       # sign-in, SQL warehouse and catalogs
python -m dbde generate-source <source> # bronze metadata -> models/sources/<source>.yml
python -m dbde show "select ..."        # one read-only query; {{ ref() }} allowed
python -m dbde build --target dev       # deploy, run silver, test, load gold, test
python -m dbde build --target dev --layer gold --skip-deploy
python -m dbde deploy --target dev      # gold SQL + bundle (notebooks, jobs, Genie)
python -m dbde test --target dev --layer silver --select Policy
python -m dbde test-genie --target dev  # run the Genie example questions
```

Use `--target dev` only. `test` and `prod` are protected; only the release pipeline writes to them. Never put hosts, ids, tokens or secrets in any file.

## Check the data before you ask the user

Before you put a question to the user, ask whether the data can answer it. Run a read-only query and report the evidence. Ask the user only what the data can't answer: what a code means, units and business rules.

- **Grain and keys:** is this combination unique? Compare `count(*)` with `count(distinct ...)`.
- **Join coverage:** how many rows have no match, and why (null key, or a key missing from the other table)?
- **Constant or changing:** is an attribute the same across the grain you plan to aggregate to?
- **Null rates and ranges:** how many nulls, and what are the min and max?

How:

- `python -m dbde show "<select ...>" --target dev`. Reference tables with `{{ ref('schema.Table') }}`, or bronze with `{{ source('<source>', '<table>') }}`. The query runs on the SQL warehouse in Databricks SQL.
- Put several checks into one query with `union all`. Return counts and aggregates, never row-level data. Never print values from PII columns.
- State the result in the question, e.g. "Answered from data: channel never varies within a policy (0 of 106,433). Recommend: include it."
- A query result tells you what the data does, not what it means. Don't invent a business rule from it.

## Personal data (PII)

Whenever you create or change a table, check every column for personal data and mark it. Don't wait to be asked.

- `meta.pii: true` for names, emails, phone numbers, addresses, postal codes, dates of birth, exact age, gender, ID numbers and anything derived from them. Mark the column in every layer it appears in.
- Health, claims and person-level financial amounts are sensitive. Don't mark them automatically. List them under "Decisions for you" with a recommendation.
- Never sample or print PII values. A PII `meta.matrix.example` must be synthetic (`Thabo M***`).
- In your summary, list the columns you marked.

## Planning and building silver integrated and gold tables

When asked to build "intermediate"/integrated silver tables (`/build-intermediate`) or gold tables (`/build-marts`), work in two phases with a stop between them:

1. **Propose, don't build.** Read the existing specs, check the data, then propose tables (name, grain, inputs, what it does) with open questions and a recommended answer for each.
2. **Build only what the user confirms.** Rules and join keys the data doesn't settle become `TODO(...)` in the spec description and are listed.

## AI enablement (metric views and Genie spaces)

The kit renders a Unity Catalog **metric view** per fact with measures, and a **Genie space** per `models/genie/<Name>.yml`. Follow `.github/instructions/genie.instructions.md`, and `.github/prompts/build-genie-space.prompt.md` for the plan-then-build flow (`/build-genie-space`).

- Never edit `gold/**/metric_views/**`, `genie/**` or `resources/genie_*.yml`. Change the spec and run `python -m dbde render` (rule R1).
- Every column and measure has a business description (meaning, unit, allowed values). Every fact Genie should answer "how much" for has at least one measure. A measure is an aggregate (rules A1, A7).
- `synonyms:` (table, column, measure) and `ai_instructions:` (table) are facts people know. Never invent them. Leave them out and list them under "Decisions for you".
- PII columns stay out of metric views (`genie.pii: exclude` in `dbde.yml`). Genie cannot hide single columns, so a space never lists a table with PII columns: use the metric view instead (rule A5). Never name PII columns in instructions or examples (rule A8).
- A space has a title, description, instructions, its tables, and three or more example questions as Databricks SQL over `{{ ref() }}` of the space's tables (rules A3, A4, A6). Metric view examples use `measure(<Name>)` and `group by all`.
- `permissions.can_run` names the real consumer group. Never invent one: leave `TODO(consumer group)` and list it (rule A9).
- Run the examples: `python -m dbde test-genie --target dev`. A failing example is a finding: report it, don't delete or weaken it.
- AI functions (`ai_query`, `ai_classify`, ...) are not rendered by the kit yet. If asked, apply the rules in `genie.instructions.md` (provenance, versioned prompts, incremental only) and tell the user it needs a toolkit change. Never hand-write the notebook.

## Connecting and scaffolding a project

- The workspace connection is set up with `.github/prompts/connect-workspace.prompt.md` (`/connect-workspace`). If `dbde.yml` still has `TODO` warehouses, or sign-in fails, ask the user in chat for the project name, workspace URL, SQL warehouse names and catalogs. Then set it up for them. Don't tell them to edit files. The workspace URL goes only into the CLI profile (`databricks auth login`), never into a repo file.
- New projects and new sources are scaffolded with `.github/prompts/scaffold-databricks-project.prompt.md`.
- `/build-genie-space` asks for the Genie consumer group. When a user gives a group later, write it into `permissions.can_run` and render.
- Never invent hosts, ids, warehouse names, groups or column meanings. If the user can't answer yet, write `TODO(...)` and report it.

## Information Matrix

The client Information Matrix (`.xlsx`) is generated by `scripts/build_information_matrix.py` from `target/manifest.json`. Follow `.github/prompts/information-matrix.prompt.md`.

- NEVER edit the workbook by hand, and never commit it (it holds sampled data). It is written to `target/`.
- Facts only people know go in `meta.matrix` on the table or column (keys in `.github/instructions/gold.instructions.md`). If you don't know a value, leave it out; the gap report lists it. Never guess.
- BUS Matrix rows come from `references` columns. Measures come from `measures:` on facts.
- When silver and gold build and their tests pass, finish by generating the matrix. Don't wait to be asked.

## How to finish every task (what the user sees)

End **every** task with this block, and nothing after it. Keep earlier chat short: one line per step.

```
<Task> finished. Details: <file, if you wrote a report>

Result: <n> tables, <n> tests (<n> pass), <n> TODOs.
Checks: dbde check <pass/fail> | dbde render <up to date/changed> | dbde build <pass/fail/not run>

Decisions for you:
1. <question>. Recommend: <your answer>.
(only things the data couldn't answer; leave this block out if there are none)

Your next steps:
1. <the single most important action, with the exact command or file>
2. ...
(at most 5, in order)

Reply "<short phrase>" and I will <what happens next>.
```

- "Your next steps" is always present, numbered, and starts with a blocking item if there is one.
- Each decision has a recommended answer. If a check failed, step 1 is how to fix it.

## Before you finish a change

- [ ] Right folder, file name and prefix; `ref()` only, no hard-coded catalogs
- [ ] Grain in the description; every column described; business key marked
- [ ] Business tests added; PII marked in every layer
- [ ] Measures described (and `synonyms` only where known); Genie examples run with `test-genie`
- [ ] `python -m dbde render` run and rendered files included
- [ ] `python -m dbde check` passes
- [ ] `python -m dbde build --target dev` passes for the changed layer (when a dev workspace is available)
- [ ] One story per pull request

## When reviewing pull requests

Flag issues in this order, citing the rule id from `databricks_engineering_standards.md`, section 12:

1. Hand edits to rendered files (R1)
2. Layer violations (L*)
3. Missing keys, tests or descriptions (S*, T*)
4. PII reaching Genie (A5, A8)
5. Naming and type violations (N*, D*)
6. Non-deterministic logic (I1)
7. Style
