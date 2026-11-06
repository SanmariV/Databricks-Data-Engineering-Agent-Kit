# START HERE: Build a Databricks data engineering project with the agent (no dbt)

This guide takes you from nothing to tested silver and gold tables and a Genie space in Databricks, using GitHub Copilot in VS Code (Claude Code works the same way: it reads `AGENTS.md`).

Databricks runs the code:

- PySpark notebooks with Delta MERGE for silver
- Unity Catalog SQL stored procedures for gold
- metric views and Genie for AI

dbt is not used. The agent writes table specs, and the `dbde` toolkit turns them into notebooks, procedures, metric views, Genie spaces and an Asset Bundle, then deploys and tests them. `dbde` stands for **Databricks Data Engineering**; you run it as `python -m dbde`, much as you would run `dbt`.

Read it top to bottom the first time. Each step says what to do, what you should see, and what to do if you don't. A worked example (insurance policies and agents) is in `examples/insurance/`; look at it whenever you wonder what a spec should look like.

## Starting a new project from this kit

1. **Clone the kit into a short folder** (not OneDrive, to avoid Windows long-path errors). In PowerShell:

```powershell
git clone <kit repo address> C:\dev\my-databricks-project
```

2. **Disconnect it from the kit and connect it to your own repo** before your first commit, so a push never goes to the kit:

```powershell
cd C:\dev\my-databricks-project
git remote set-url origin <your project repo address>
git remote -v
```

   `git remote -v` must print your project address twice. To keep it local only for now, run `git remote remove origin` instead.

3. **Publish it to your repo:** `git push -u origin main`. For a fresh history instead, delete `.git`, then run `git init -b main`, commit, add the remote and push.

4. **Open the folder in VS Code** (File → Open Folder) and follow this guide from step 1.

## The whole journey at a glance

| # | Step | You run | Result |
| --- | --- | --- | --- |
| 1 | VS Code settings (once per computer) | Edit the VS Code settings file | Copilot reads `AGENTS.md` and shows `/` prompts |
| 2 | Install the tools | `pip install -r requirements.txt`, Databricks CLI | `python -m dbde --help` works |
| 3 | Catalogs and bronze | Catalog Explorer (or an admin) | `dev_bronze` (data loaded), `dev_silver`, `dev_gold` |
| 4 | Connect | **`/connect-workspace`** (asks for the URL, warehouse and catalogs) | `dbde.yml` filled in, signed in, connection tested |
| 5 | Permissions and compute | Workspace admin, once | Serverless jobs, SQL warehouse, rights to create schemas |
| 6 | Scaffold | **`/scaffold-databricks-project`** | Source specs and one silver table per bronze table, built and tested |
| 7 | Integrated silver | **`/build-intermediate`** | Joined, business-rule silver tables |
| 8 | Gold | **`/build-marts`** | `Dim` and `Fact` tables with load procedures, plus metric views |
| 9 | Health check | **`/project-check`** | List of problems to fix |
| 10 | Genie | **`/build-genie-space`** | A Genie space over gold, tested |
| 11 | Information Matrix | **`/information-matrix`** | An Excel workbook in `target/` |
| 12 | Schedule and release | `dbde.yml`, release pipeline | The daily job runs as a service principal in test and prod |

There is no pipeline to build by hand: the daily job is rendered from the specs (`resources/dbde_daily_load.job.yml`) and deployed with the bundle.

Always work in **Copilot Chat, Agent mode**, start a **new chat for each prompt**, and use a strong model (for example Claude Sonnet or Opus, or the top-tier GPT models). Small, fast models miss rules and give up on problems.

## How the agent works (read this once)

- **`AGENTS.md`** holds the rules the agent follows: layers, naming, tests, PII, Genie and how to check data. It is read automatically.
- **You decide, the agent proposes.** Business rules, units, consumer groups and the meaning of codes are yours. The agent writes `TODO(...)` where it can't tell, and never invents keys, ids or meanings.
- **Specs in, code out.** The agent writes `models/**/*.yml` and `models/**/*.sql`. `python -m dbde render` writes:
  - `notebooks/` and `gold/`
  - `genie/`
  - `databricks.yml` and `resources/`
  These files are committed so reviewers see what runs, but nobody edits them by hand.
- **Every prompt ends with "Your next steps".** Do those in order.
- **Use the `dev` target only.** `test` and `prod` refuse writes from a laptop.

## 1. Turn on the VS Code settings

A one-time change to VS Code itself:

1. `Ctrl+Shift+P` → **Preferences: Open User Settings (JSON)**.
2. Add these two lines inside the `{ }` (every line except the last needs a comma):

```json
"chat.useAgentsMdFile": true,
"chat.promptFiles": true
```

3. Save. If `chat.promptFiles` shows "Unknown configuration setting", delete that line.

**Check it worked.** Open Copilot Chat (`Ctrl+Alt+I`), switch to **Agent**, and type `/`. You should see these prompts:

- `connect-workspace`
- `scaffold-databricks-project`
- `build-intermediate`
- `build-marts`
- `build-genie-space`
- `project-check`
- `information-matrix`

## 2. Install the tools

1. **Python 3.10 or later.** `python --version`.
2. **Databricks CLI 1.3.0 or later** (bundle deploys, sign-in):

```powershell
winget install Databricks.DatabricksCLI
databricks --version
```

3. **The kit's Python packages**, from the repo root:

```powershell
pip install -r requirements.txt
python -m dbde --help
python -m pytest -q
```

   The tests run offline against `examples/insurance` and should all pass.

   If `python -m dbde` fails with `This must be an MFC application`, an old checkout is using the name `dde`, which clashes with the pywin32 `dde` module. This kit's package is `dbde`.

## 3. Catalogs and bronze

You need a Unity Catalog-enabled workspace with serverless compute. Create three catalogs for dev (or ask a metastore admin to):

| Catalog | Holds | Who creates the schemas |
| --- | --- | --- |
| `dev_bronze` | One schema per source system, landed by ingestion | Ingestion |
| `dev_silver` | Silver tables | The kit (`CREATE SCHEMA IF NOT EXISTS`) |
| `dev_gold` | Gold tables, procedures, metric views, `ops` | The kit |

Other names are fine: set them in `dbde.yml` under `targets.dev.catalogs`.

One catalog with `bronze`, `silver` and `gold` schemas works too (`life_insurance.silver.Policy`). Set every layer of `targets.dev.catalogs` to that catalog and add `layer_schemas: {bronze: bronze, silver: silver, gold: gold}` to `dbde.yml`. Spec folders still group the specs, but every silver table goes into the `silver` schema and every gold table into the `gold` schema, so table names must be unique per layer (`dbde check`, rule N5). The data quality objects stay in `<catalog>.ops`.

### 3a. Sandbox: bronze from CSV files

No ingestion yet? Create a schema per source in `dev_bronze`, then upload CSVs with **Catalog Explorer → Create → Table** (one table per file). Add an `_ingested_at` timestamp column if you can (`current_timestamp()` at upload); without it, use `load: {mode: full}` and drop the freshness rule. The upload wizard infers types; that is fine for bronze.

## 4. Connect the toolkit to the workspace: `/connect-workspace`

The agent asks you for the values it needs, writes `dbde.yml`, signs you in and tests the connection. You don't edit any file yourself.

1. New chat (Agent mode) → `/connect-workspace`.
2. It asks for:
   - your **project name** (snake_case, e.g. `colina_analytics`)
   - the **dev workspace URL**: copy it from the browser address bar in Databricks, up to `.net` or `.com`
   - the **dev SQL warehouse name**: Databricks → SQL Warehouses, copied exactly (e.g. `Serverless Starter Warehouse`)
   - the **test and prod SQL warehouse names**, or `skip` if you don't know them yet
   - the **catalog names**, or `default` for `dev_bronze` / `dev_silver` / `dev_gold`
3. It runs `databricks auth login --profile dbde-dev`. **A browser window opens: sign in there, then go back to VS Code.** The sign-in is stored in your Databricks CLI profile; the workspace URL and tokens never go into the repo.
4. It runs `python -m dbde debug --target dev`. You should see your user, the three catalogs and `debug: connection OK`. If a catalog or a permission is missing, it tells you which step below to do.

You can skip this step: `/scaffold-databricks-project` (step 6) asks the same questions when it finds the workspace isn't connected. Run `/connect-workspace` again whenever a warehouse or catalog changes.

## 5. Permissions and compute (once, with an admin)

- Serverless compute for jobs and notebooks is enabled for the workspace.
- You have `CAN USE` on the SQL warehouse.
- On the dev catalogs you have:
  - `USE CATALOG` and `CREATE SCHEMA` on `dev_silver` and `dev_gold`
  - `USE CATALOG`, `USE SCHEMA` and `SELECT` on `dev_bronze`
- For Genie: Genie is enabled in the workspace, and you can create spaces.

## 6. Scaffold the project: `/scaffold-databricks-project`

This step creates the source specs and one silver source table per bronze table, then deploys, runs and tests them.

1. In a new chat (Agent mode), type `/scaffold-databricks-project`.
2. Answer its inputs:
   - the project name
   - the bronze source systems (schema names in `dev_bronze`)
   - the gold domains (for example `sales, distribution`)
   - an Information Matrix path, or `none`
   - a **data dictionary path**, if you have one. The dictionary's text becomes the column descriptions; anything it doesn't cover becomes `TODO(...)`.
3. It ends with a short summary and **Your next steps**. The full detail is in `docs/scaffold_report.md`.

Then check for yourself:

```powershell
python -m dbde check
python -m dbde test --target dev --layer silver
```

In Databricks, open `dev_silver` in Catalog Explorer. You should see one schema per source, with the silver tables. Each table has the four audit columns at the end, a primary key and column comments. **Workflows → Jobs** shows `[dev <you>] <project>_daily_load`, with one task per silver table.

## 7. Integrated silver tables: `/build-intermediate`

Integrated silver tables hold joins and business rules (dbt calls this the intermediate layer). The prompt runs in two phases, with a stop between them:

1. New chat → `/build-intermediate` → your domain (for example `sales`).
2. **Phase 1: it proposes and changes no files.** It suggests tables and lists open questions, each with evidence from a counts-only query and a recommended answer.
3. **You decide.** Reply, for example, `build 1 and 2`, and answer what you can.
4. **Phase 2: it builds** the confirmed tables (spec + Spark SQL), renders, deploys, runs and tests them.

## 8. Gold tables: `/build-marts`

Gold holds the `Dim` and `Fact` tables people query. The same two phases apply:

1. New chat → `/build-marts` → your domain.
2. It plans:
   - dimensions, with SCD type 1 or 2
   - facts, with grain, dimension keys and measures
   It checks the data, lists the columns it would mark as personal data, and stops.
3. Reply `build 1 and 2`. For each table, it writes the spec and the Databricks SQL SELECT. Then it renders the table DDL, the `Load<Table>` procedure and the metric view, and deploys, loads and tests.

Check in Catalog Explorer: open `dev_gold`, schema `<domain>`.

- Every dimension has a row with `_SK = -1` (the unknown member). Facts point there when a key has no dimension row, and `python -m dbde test` reports how many.
- Each fact with measures has a `Metric<Process>` metric view.
- The `Load<Table>` procedures are listed under the schema.

## 9. Check the project: `/project-check`

New chat → `/project-check`. It runs `python -m dbde check` and the dev data tests, then lists what to fix, most serious first. The check covers the layer, naming, type and completeness rules, and whether the rendered files are up to date. It fixes nothing until you reply, for example `fix 1`.

## 10. Genie: `/build-genie-space`

After gold builds and its tests pass: new chat → `/build-genie-space` → a space name (for example `SalesAnalyst`) and the **consumer group** that may use it. Give the exact group name from Settings → Identity and access → Groups, or `later`.

The agent first proposes, then stops:

- the measures
- the tables and metric views the space reads
- what stays out because of personal data
- the questions the space should answer

Reply `build all`. It writes the measures and `models/genie/<Name>.yml`, then renders:

- `gold/<domain>/metric_views/Metric<Process>.sql`, the metric views (PII columns left out)
- `genie/<Name>.geniespace.json`, the space: instructions, tables, example SQL and benchmarks
- `resources/genie_<name>.yml`, the bundle resource that creates the space on your SQL warehouse
- `gold/ops/grants/<Name>.sql`, the `SELECT` grants for the consumer group

Then, in your own terminal:

```powershell
python -m dbde deploy --target dev        # gold SQL (metric views, grants), then the bundle (Genie space)
python -m dbde test-genie --target dev    # runs every example question read-only
```

If you answered `later` for the group, reply with the group name in the same chat before you deploy: the deploy fails on `TODO(consumer group)`. In Databricks, open the space under **Genie**, ask it the example questions and run the benchmarks. Compare the answers with `test-genie`.

## 11. Build the Information Matrix: `/information-matrix`

After silver and gold build and `/project-check` is clean: new chat → `/information-matrix`. Give it:

- a version (for example `Dev`)
- your name
- `none` for the previous matrix

It builds `target/Information Matrix Dev.xlsx` from the specs, profiles the data read-only (counts and non-PII examples), and lists what only people can fill in. Never edit or commit the workbook. See `information_matrix_instructions.md`.

## 12. Schedule and release

- **Schedule.** `daily_load.schedule` in `dbde.yml` (Quartz cron, default 05:00 UTC). Set it to after bronze ingestion finishes, add `notify_on_failure`, and run `python -m dbde render`. In dev, the bundle's development mode pauses schedules.
- **Run by hand:** `python -m dbde run --target dev` (all tasks), or `--layer gold`, or `--load-date 2026-11-05` for a rerun.
- **Release (test, prod).** A release pipeline, not a laptop, runs this as the release service principal:

```bash
export DATABRICKS_HOST=... DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=...   # pipeline secrets
export DBDE_ALLOW_PROTECTED=1
export BUNDLE_VAR_run_as_service_principal=<application id>
python -m dbde build --target test
```

  Set `targets.test.warehouse` and `targets.prod.warehouse` in `dbde.yml` first. See the standards, section 9.4.

## Rules the agent follows (what to expect)

- **Layers:** silver source tables read one bronze table; integrated silver reads silver; gold reads silver and gold; all through `{{ ref() }}`. Nothing hard-codes a catalog.
- **No hand-written notebooks, procedures or Genie JSON.** Specs and SELECTs only; `dbde render` writes the code, to DE Policy v2.
- **Personal data:** marked `meta.pii: true` in every layer, automatically. Sensitive amounts are listed for your decision, and values are never printed. PII never reaches Genie.
- **Check the data first:** before asking you, the agent runs a read-only counts query with `python -m dbde show` and shows the numbers.
- **No guessing:** unknown keys, ids, groups and meanings become `TODO(...)` in the report.

## Daily commands

```powershell
python -m dbde check                                   # rules + render drift (offline)
python -m dbde render                                  # regenerate notebooks, SQL, Genie, bundle
python -m dbde build --target dev                      # deploy, run silver, test, load gold, test
python -m dbde build --target dev --layer gold --skip-deploy
python -m dbde test --target dev --select FactNewBusiness
python -m dbde test-genie --target dev                 # Genie example questions
python -m dbde show "select count(*) as n from {{ ref('sales.FactNewBusiness') }}"
python -m dbde ls --tests
```

## Troubleshooting

| What you see | Cause | Fix |
| --- | --- | --- |
| `R1 out of date: ...` in `dbde check` | A spec changed (or someone edited a rendered file) | `python -m dbde render`, commit the result |
| `set targets.dev.warehouse in dbde.yml` | No SQL warehouse name | Run `/connect-workspace` (step 4) |
| `Databricks sign-in failed` | No or expired CLI profile | Run `/connect-workspace` again, or `databricks auth login --host <url> --profile dbde-dev` |
| `SQL warehouse '<name>' not found` | Name differs from the workspace | Copy the exact name from SQL Warehouses |
| `No module named databricks_de_common` in a notebook run | Notebook run outside the job without the bundle files | Run through the job (`python -m dbde run`), or open the notebook from the bundle folder in the workspace |
| Bundle deploy fails on the Genie space | `TODO(consumer group)` or Genie not enabled | Tell the agent the group name (it sets `permissions.can_run`); ask an admin to enable Genie |
| `CREATE PROCEDURE` is not supported | SQL stored procedures not available on the warehouse | Standards, section 15, spike item 1 |
| Source freshness fails in dev | Dev bronze is older than 48 hours | `--skip-source-tests` in dev; never in test or prod |
| `target 'test' is protected` | Writes to test/prod only come from the release pipeline | Use `--target dev` |
| `/` shows no prompts | Settings or folder not loaded | Redo step 1; reload VS Code |

Reference docs: `AGENTS.md` (rules), `databricks_engineering_standards.md` (rationale and spike list), `docs/architecture.md` (how the parts fit), `examples/insurance/` (a complete worked example).
