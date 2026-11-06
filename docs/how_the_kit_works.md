# How the kit works: a plain-language overview

This page is for someone seeing the kit for the first time. It explains what
the parts are, how they fit together and where the AI agent comes in. It does
not explain how to set anything up. For that, read
[`START_HERE.md`](../START_HERE.md). If you know dbt, also read
[`architecture.md`](architecture.md), which maps each part to its dbt
equivalent.

---

## 1. The idea in one paragraph

You **describe** each table you want in a short YAML file: what one row means,
which columns it has, what each column means and which checks the data must
pass. A small Python toolkit called **`dbde`** (Databricks Data Engineering)
turns those descriptions into the code Databricks runs:

- PySpark notebooks for silver
- SQL stored procedures for gold
- metric views and Genie spaces for AI
- the jobs that run it all every day

It then deploys that code, runs it and tests the result. The **AI agent**
(GitHub Copilot, or another coding agent) writes and changes the YAML files
for you, following a written rulebook. It never writes the Databricks code
itself. dbt is not used.

> **Analogy.** The YAML file is the architect's drawing. `dbde` is the
> construction crew that builds exactly what the drawing shows, the same way
> every time. The agent is a junior architect who drafts the drawings by the
> firm's rulebook and asks you whenever something needs a business decision.

---

## 2. The problem it solves

Building a data platform by hand means writing hundreds of notebooks and stored
procedures that look almost the same. Each one needs the same MERGE logic,
audit columns, keys, history handling and tests. Copy-paste brings drift and
mistakes, and the standards (Calybre DE Standards Policy v2) are hard to apply
the same way every time.

dbt solves part of this, but on Databricks it has no notebooks and no stored
procedures, which the policy asks for. This kit generates exactly those, so
no exceptions to the policy are needed.

The kit splits the work in two:

| The part that differs per table | The part that is the same for every table |
| --- | --- |
| Which columns there are, what they mean, how they are calculated, which business rules apply, which measures and questions matter | How to MERGE, add `_SK` keys and audit columns, keep SCD2 history, run tests, build metric views and Genie spaces, and deploy |
| **Written by people and the agent, in YAML and SQL** | **Written once, in `dbde`, and generated for every table** |

---

## 3. The data flow (where the data goes)

```
Source systems ─► BRONZE ─► SILVER (source) ─► SILVER (integrated) ─► GOLD ─► Metric views ─► Genie
               <env>_bronze   <env>_silver          <env>_silver        <env>_gold   <env>_gold
```

Each layer has its own Unity Catalog **catalog** per environment, for example
`dev_silver` and `prod_gold`. The code is the same in every environment; only
the catalog names change.

| Layer | Where | What happens there | Who builds it |
| --- | --- | --- | --- |
| **Bronze** | Catalog `<env>_bronze` | Raw data, as it arrived from the source | Ingestion pipelines (outside this repo; the kit only reads it) |
| **Silver source tables** | Catalog `<env>_silver` | One clean table per bronze table: rename, cast types, trim, upper-case codes. No joins. | The kit (one PySpark notebook per table) |
| **Silver integrated tables** | Catalog `<env>_silver` | Joins and business rules across silver tables | The kit (a PySpark notebook running your Spark SQL) |
| **Gold** | Catalog `<env>_gold` | Star schema: `Dim`, `Fact` and `Bridge` tables with surrogate keys and history | The kit (one SQL stored procedure per table) |
| **Metric views** | Catalog `<env>_gold` | The measures of a fact ("how many", "how much"), ready to slice by its dimensions | The kit, from the measures on each fact |
| **Genie** | Genie space | Business users ask questions in plain language over gold | The kit renders the space; a person tries it and shares it |

The layer rules keep this clean. Silver never reads gold, and a source table
never joins. Tables are always referenced as `{{ ref('schema.Table') }}`,
never by hard-coded catalog names.

---

## 4. The building blocks

Think of the kit as six kinds of parts:

```
 ┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
 │ 1. RULEBOOK           │   │ 2. PROMPTS            │   │ 3. SPECS (YAML + SQL) │
 │ AGENTS.md             │──►│ /connect-workspace    │──►│ models/**             │
 │ .github/instructions  │   │ /scaffold-...         │   │ what each table and   │
 │ standards document    │   │ /build-marts  ...     │   │ Genie space is        │
 └───────────────────────┘   └───────────────────────┘   └───────────┬───────────┘
   how the agent behaves        step-by-step recipes                  │ read by
                                                                      ▼
 ┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
 │ 6. DATABRICKS         │◄──│ 5. RENDERED CODE      │◄──│ 4. THE dbde TOOLKIT   │
 │ catalogs, SQL         │   │ notebooks/ (silver)   │   │ python -m dbde        │
 │ warehouse, jobs,      │   │ gold/**   (gold, AI)  │   │ parse, check, render, │
 │ Genie                 │   │ genie/, bundle, jobs  │   │ build, test, show     │
 └───────────────────────┘   └───────────────────────┘   └───────────────────────┘
   where it runs                generated, never edited      the engine
```

### 1. The rulebook: how the agent must behave

| File | What it holds |
| --- | --- |
| [`AGENTS.md`](../AGENTS.md) | The rules the agent reads on every request: layers, naming, tests, PII, Genie, "check the data before you ask", and how to finish a task |
| [`.github/instructions/`](../.github/instructions/) | Extra rules that apply to particular folders (sources, silver, gold, Genie, specs) |
| [`databricks_engineering_standards.md`](../databricks_engineering_standards.md) | The full standard and its reasoning, mapped to Calybre DE Standards Policy v2 |

### 2. The prompts: step-by-step recipes for the agent

Each prompt in [`.github/prompts/`](../.github/prompts/) is a slash command
you type in Copilot Chat. Each one is a scripted task with a fixed set of
steps. When a prompt needs something only you know, such as your workspace
URL, a SQL warehouse name or a consumer group, it asks you in the chat and
fills it in for you.

| Prompt | What the agent does |
| --- | --- |
| `/connect-workspace` | Asks for your project name, workspace URL, SQL warehouse and catalogs, writes `dbde.yml`, signs you in and tests the connection |
| `/scaffold-databricks-project` | Reads bronze, writes source specs and one silver table per bronze table |
| `/build-intermediate` | Proposes, then builds, integrated silver tables (joins, business rules) |
| `/build-marts` | Proposes, then builds, gold `Dim` / `Fact` tables |
| `/build-genie-space` | Proposes, then builds, measures and a Genie space; asks who may use it |
| `/project-check` | Health check of the whole project |
| `/information-matrix` | Builds the client Information Matrix workbook |
| `/data-architecture-diagram` | Draws the architecture |

### 3. The specs: the single source of truth

Everything about a table lives in `models/`. The folder is the schema, and the
file name is the table name.

| File | Describes |
| --- | --- |
| `models/sources/<source>.yml` | The bronze tables of one source system and how fresh they must be |
| `models/silver/<source>/<Table>.yml` | A silver source table: one `expression` per column, read from one bronze table |
| `models/silver/<domain>/<Table>.yml` + `.sql` | An integrated silver table: spec plus a Spark SQL SELECT |
| `models/gold/<domain>/<Table>.yml` + `.sql` | A gold table: spec plus a Databricks SQL SELECT; facts list their measures |
| `models/genie/<Name>.yml` | A Genie space: who it is for, instructions, tables, example questions, consumer group |
| `dbde.yml` | Project settings: environments (CLI profile, SQL warehouse name, catalogs) and schedules. No secrets |

### 4. The `dbde` toolkit: the engine

`dbde` is a small Python package in [`dbde/`](../dbde/). You run it as
`python -m dbde <command>`.

| Command | What it does, in plain words |
| --- | --- |
| `parse` | Reads all the specs and writes a map of the project (`target/manifest.json`) |
| `check` | Checks every rule (naming, keys, descriptions, layers, PII in Genie) and confirms nobody hand-edited generated files |
| `render` | **Turns the specs into Databricks code** |
| `debug` | Tests the sign-in, the SQL warehouse and the catalogs |
| `show "select ..."` | Runs one read-only query, so you or the agent can look at the data |
| `generate-source <source>` | Reads bronze metadata and drafts a source spec |
| `build --target dev` | Deploys, runs silver, tests it, loads gold and tests it |
| `deploy`, `run`, `test` | Deploys only, runs the daily job only, or runs only the tests |
| `test-genie` | Runs every example question of the Genie spaces, read-only |

### 5. The rendered code: what Databricks runs

`render` writes these files. They are committed to Git so reviewers can see
exactly what runs, but **nobody edits them by hand**. `dbde check` fails if
anyone does (rule R1).

| Generated from the specs | What it is |
| --- | --- |
| `notebooks/silver/nb_silver_*.py` | One PySpark notebook per silver table: read, cast, deduplicate, Delta MERGE, audit columns |
| `gold/<domain>/tables/<Table>.sql` | The gold table definition, with `_SK` key, `StartDate` / `EndDate` / `IsCurrent`, audit columns, comments and keys |
| `gold/<domain>/procedures/Load<Table>.sql` | The stored procedure that MERGEs your SELECT into the table (with SCD2 history for type 2 dimensions) |
| `gold/<domain>/metric_views/Metric<Process>.sql` | A metric view per fact with measures |
| `gold/ops/**` | Data-quality test procedures, the results table and Genie grants |
| `genie/<Name>.geniespace.json` | The Genie space: instructions, tables, example SQL |
| `databricks.yml`, `resources/*.yml` | The Asset Bundle: environments, the daily load job, the weekly maintenance job, the Genie spaces |

The shared notebook code (MERGE, deduplication, audit columns) lives once in
[`src/databricks_de_common/`](../src/databricks_de_common/). Every silver
notebook imports it.

### 6. Databricks: where it runs

- **Unity Catalog:** three catalogs per environment (bronze, silver and gold).
- **A SQL warehouse:** runs the gold SQL, the tests, the toolkit's queries and Genie.
- **Two jobs on serverless compute:**
  - The daily load: source tests, then silver, silver tests, gold and gold tests, in dependency order.
  - A weekly job that runs OPTIMIZE and VACUUM on every table.
- **Genie spaces**, for business users.

The jobs are generated from the specs, so adding a table adds its task
automatically. Nobody builds a pipeline by hand.

---

## 5. One table, end to end

Take the insurance example in [`examples/insurance/`](../examples/insurance/).

**Step 1: the silver spec** (`models/silver/policy_admin/Agent.yml`). It says
where the data comes from and how each column is cleaned:

```yaml
description: One row per agent as currently held in the policy admin system. Grain = Agent_BK.
source: {source: policy_admin, table: agent}
columns:
  - name: Agent_BK
    data_type: string
    expression: upper(trim(agent_code))
    business_key: true
    description: Agent code from the policy admin system.
  - name: AgentName
    data_type: string
    expression: trim(agent_name)
    description: Agent full name.
    meta: {pii: true}
```

**Step 2: the gold spec and SELECT** (`models/gold/distribution/DimAgent.yml`
and `.sql`). The spec says this is a history-keeping dimension. The SELECT
says where the rows come from:

```yaml
description: One row per agent version (SCD type 2). Grain = Agent_BK, StartDate.
scd_type: 2
columns:
  - name: Agent_BK
    data_type: varchar(20)
    business_key: true
    description: Agent code from the policy admin system.
```

```sql
select
    agent.Agent_BK,
    agent.AgentName,
    agent.BranchCode,
    agent.IsActive
from {{ ref('policy_admin.Agent') }} as agent
```

**Step 3: `python -m dbde render`** writes these files, without anyone typing them:

- `notebooks/silver/nb_silver_policy_admin_agent.py`: the PySpark MERGE notebook
- `gold/distribution/tables/DimAgent.sql`: the table, with `DimAgent_SK`,
  `StartDate`, `EndDate`, `IsCurrent` and the four audit columns added
- `gold/distribution/procedures/LoadDimAgent.sql`: the SCD2 MERGE procedure
- a task for each in the daily job (`resources/dbde_daily_load.job.yml`)

**Step 4: `python -m dbde build --target dev`** deploys it, runs it and tests
it. The kit adds these tests on its own:

- keys are not null and unique
- each agent has exactly one current version, and no versions overlap
- fact keys point to real dimension rows

The spec can add business tests on top, such as allowed values and ranges.

**Step 5: the same specs feed the AI layer.** The fact `FactNewBusiness`
references `DimAgent` and declares two measures, so `render` also writes:

- the metric view `MetricNewBusiness`: new business count and premium, sliceable by product, issue date, branch and agent status
- the Genie space `SalesAnalyst` (from `models/genie/SalesAnalyst.yml`): it reads the metric view, and its example questions are tested with `dbde test-genie`

`pii: true` keeps `AgentName` out of the metric view, and the check refuses a
Genie space that lists `DimAgent` directly, because Genie cannot hide a single
column. The descriptions become the Unity Catalog comments that Genie reads,
and they fill the Information Matrix.

> **Write once, use many times:** one spec produces the notebook or procedure,
> the job task, the tests, the documentation, the metric view, the Genie space
> and the Information Matrix.

---

## 6. How the agent uses the YAML files

The agent is useful here because the YAML files are small, structured and
checkable. Without them, the agent would have to write and read long notebooks
and procedures. Here is what it does with them.

### It asks you for what only you know, in the chat

Some values can't be found in the data or the code: your workspace URL, the
name of your SQL warehouse, your catalog names and the group that may use a
Genie space. The prompts ask you for these in the Copilot chat and write them
into the right file for you. You never edit `dbde.yml` by hand. The workspace
URL goes only into your Databricks CLI sign-in, never into the repo. If you
don't know a value yet, say "later". The agent marks it `TODO(...)` and puts it
in your next steps.

### It reads the specs to understand the project

Before it changes anything, the agent reads the existing specs. They tell it:

- which tables exist
- what each row means (the `Grain = ...` line)
- which columns are keys
- how tables connect (`ref()` and `references:`)

This is far more reliable than reading generated code, because every fact is
stated once in a fixed format.

### It checks the data before it asks you

The agent uses `python -m dbde show` to run read-only queries on the SQL
warehouse. Before it asks you something, it checks whether the data can answer
it: is this key unique, how many rows fail to join, is this column ever null?
It then puts the evidence in the question, for example:
"Channel never varies within a policy (0 of 106,433). Recommend: include it."
It asks you only what the data can't answer, such as what a code means or
which business rule applies.

### It writes or changes specs, never Databricks code

When you ask for a table or a Genie space, the agent writes the `.yml` (and the
`.sql`, if needed). It is not allowed to write any of these:

- notebooks or stored procedures
- `MERGE` or `CREATE TABLE` statements
- metric views, Genie JSON or job definitions

If something can't be expressed in a spec, it must stop and say so. This keeps
generated code identical everywhere and keeps the agent out of the riskiest
code.

### The toolkit checks the agent's work

After every change, the agent runs:

1. `python -m dbde render`: generates the code from its specs
2. `python -m dbde check`: fails on a missing description, a bad name, a
   missing business key, a layer violation, personal data in Genie or a
   hand-edited file
3. `python -m dbde build --target dev`: deploys, runs and tests in dev

If a check fails, the agent fixes the **spec**, not the generated code, and
runs the checks again. If a data test or a Genie example fails, it reports it
as a finding and does not weaken the test.

### It proposes first, then builds what you confirm

For integrated silver tables, gold tables and Genie spaces, the agent works in
two phases:

```
 You: /build-marts
   │
   ▼
 Agent: reads specs ─► checks data with dbde show ─► PROPOSES tables
        (name, grain, inputs, open questions, a recommended answer for each)
   │
   ▼  ── STOP: you review and decide ──
   │
 You: "build 1 and 2"
   │
   ▼
 Agent: writes YAML + SQL ─► dbde render ─► dbde check ─► dbde build ─► reports
        (anything it couldn't settle is marked TODO(...) in the spec)
```

You keep the decisions that need business knowledge. The agent does the
typing, the checking and the rule-following.

### It ends every task the same way

Every task ends with the same summary block:

- what was built
- how many tests passed
- the result of each check
- the decisions waiting for you, each with a recommendation
- your next steps, numbered

You always know where things stand and what to do next.

---

## 7. Who does what

| | You (the engineer) | The agent | The toolkit (`dbde`) |
| --- | --- | --- | --- |
| Workspace URL, warehouse, catalogs, consumer groups | **Gives them** when asked | Asks in chat, writes `dbde.yml` and specs | Uses them to deploy |
| Business meaning, rules, code values | **Decides** | Asks, recommends, marks `TODO` | — |
| Specs and SELECTs | Reviews and approves | **Writes** | Parses and validates |
| Notebooks, procedures, DDL, metric views, Genie, jobs | Reviews in the pull request | Never writes | **Generates** |
| Standards and naming | — | Follows `AGENTS.md` | **Enforces** (`dbde check`) |
| Deploying and running | Lets the daily job run | Runs `build` in dev | **Deploys, runs, tests** |
| Genie answers | Tries the space, shares it | Writes and tests the example questions | Renders and deploys the space |
| Test and prod | Merge approval | Never touches | Release pipeline only |

---

## 8. Guardrails, summarised

- **Specs are the only thing people and the agent edit.** Everything else is
  generated, and `dbde check` catches hand edits.
- **Rules are enforced by code, not by memory.** Naming, keys, descriptions
  and layer rules are checked on every commit and in CI
  (`.github/workflows/dbde_ci.yml`).
- **The agent never invents.** Unknown meanings, ids, warehouses, groups and
  synonyms become a question for you or a `TODO(...)`.
- **No secrets in the repo.** Sign-in uses your Databricks CLI profile on a
  laptop and a service principal in the release pipeline.
- **Personal data is marked** (`meta.pii: true`) in every layer. It is never
  printed, and it never reaches metric views or Genie.
- **Only `dev` is writable from a laptop.** `test` and `prod` are written only
  by the release pipeline.
- **Failing tests are findings.** They are reported, never deleted or weakened.

---

## 9. Where to go next

| If you want to... | Read |
| --- | --- |
| Set up and run the kit step by step | [`START_HERE.md`](../START_HERE.md) |
| Compare it with dbt, or see the detailed diagrams | [`architecture.md`](architecture.md) |
| Know the exact rules the agent follows | [`AGENTS.md`](../AGENTS.md) |
| Know the full standard and its policy mapping | [`databricks_engineering_standards.md`](../databricks_engineering_standards.md) |
| See a complete worked project | [`examples/insurance/`](../examples/insurance/) |
