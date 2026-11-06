# Databricks Data Engineering Agent Kit (no dbt)

A starter kit for using GitHub Copilot (or another coding agent) to scaffold and build the silver and gold layers and Genie spaces in **Databricks without dbt**. It is the no-dbt counterpart of the Databricks dbt agent kit and the Databricks twin of the Fabric agent kit. The agent workflow, the prompts and the quality bar are the same. The engine is a small Python toolkit, `dbde`, plus native Databricks objects. `dbde` stands for **Databricks Data Engineering**; you run it as `python -m dbde`, much as you would run `dbt`.

**New here?** Read [`docs/how_the_kit_works.md`](docs/how_the_kit_works.md) for a plain-language overview of the parts, [`docs/architecture.md`](docs/architecture.md) for the technical detail, then [`START_HERE.md`](START_HERE.md). It walks you through setup and every prompt, step by step.

## How it works

```
models/  (you and the agent write these)            dbde render         Databricks runs these
  sources/<source>.yml        bronze tables       ──────────────►   (generated, committed, never hand-edited)
  silver/<source>/<T>.yml     1:1 bronze cleanup                      notebooks/silver/nb_silver_*.py   PySpark + Delta MERGE
  silver/<domain>/<T>.yml+sql joins, business rules                   notebooks/ops/nb_ops_*.py
  gold/<domain>/<T>.yml+sql   Dim / Fact / Bridge                     gold/**   table DDL, Load<T> procedure,
                                                                                metric views, ops tests, grants
  genie/<Name>.yml            Genie spaces                            genie/<Name>.geniespace.json
                                                                      databricks.yml, resources/*.yml   bundle + jobs
```

- **Silver**: Delta tables in the silver catalog. Each table gets one rendered PySpark notebook. It reads, casts, deduplicates and MERGEs, adds the DE Policy v2 audit columns, and keeps a Unity Catalog primary key and comments.
- **Gold**: Delta tables in the gold catalog. Each table gets one rendered `Load<Table>` Unity Catalog SQL stored procedure (MERGE, with SCD2 for type 2 dimensions), `_SK` keys, an unknown member row, and primary and foreign keys.
- **AI**:
  - Every fact with measures gets a **metric view** (`Metric<Process>`).
  - Each `models/genie/<Name>.yml` becomes a **Genie space** over gold tables and metric views, with instructions, example SQL and benchmarks.
  - PII never reaches Genie.
- **Tests**:
  - Implied tests on every table: keys, uniqueness, SCD2 integrity and relationships.
  - Tests declared in the spec: accepted values, ranges, expressions and reconciliations.
  - The same SQL runs from your laptop (`dbde test`) and in the job (`ops.RunDataQualityTests`), which stops the load when an error-severity test fails.
- **Orchestration and deployment**:
  - One rendered Lakeflow job runs source tests, then silver, silver tests, gold and gold tests, in dependency order on serverless compute.
  - A weekly job runs OPTIMIZE and VACUUM.
  - A Databricks Asset Bundle deploys the notebooks, jobs and Genie spaces.
- **Calybre DE Standards Policy v2 is met directly.** The kit renders notebooks in silver and one stored procedure per gold table, so no TEC exceptions are needed (the dbt kit needs them).

## What's included

| Path | What it is |
| --- | --- |
| [`AGENTS.md`](AGENTS.md), [`.github/copilot-instructions.md`](.github/copilot-instructions.md), [`.github/instructions/`](.github/instructions/) | The rules the agent follows |
| [`.github/prompts/`](.github/prompts/) | `/connect-workspace`, `/scaffold-databricks-project`, `/build-intermediate`, `/build-marts`, `/build-genie-space`, `/project-check`, `/information-matrix`, `/data-architecture-diagram` |
| [`databricks_engineering_standards.md`](databricks_engineering_standards.md) | The standard: architecture, layer rules, rendered code, tests, deployment, check rules, Policy v2 mapping and spike list |
| [`dbde/`](dbde/) | The toolkit: `python -m dbde parse / check / render / ls / debug / generate-source / show / deploy / run / build / test / test-genie` |
| [`dbde/genie.py`](dbde/genie.py) | AI enablement: renders metric views, Genie serialized spaces, bundle resources and grants |
| [`src/databricks_de_common/`](src/databricks_de_common/) | Shared notebook library, deployed with the bundle |
| [`dbde.yml`](dbde.yml) | Project settings: targets (CLI profile, SQL warehouse, catalogs), schedules |
| [`scripts/build_information_matrix.py`](scripts/build_information_matrix.py) | Information Matrix workbook from `target/manifest.json` |
| [`examples/insurance/`](examples/insurance/) | A complete worked example: specs, SELECTs, a Genie space and all rendered output |
| [`tests/`](tests/) | Offline tests of the toolkit: rendered SQL parsed, notebooks compiled, rules exercised |

## Relationship to the Calybre DE Standards Policy v2

The policy is in `v2_Data Engineering Standards Policy.docx` (not committed). Section 14 of the standards maps every rule:

| Policy v2 | This kit |
| --- | --- |
| Bronze: previous-day parameter, pipelines over code | Out of scope (ingestion). The daily job's `load_date` defaults to yesterday. |
| Silver: Delta, logical primary key, explicit types, Delta MERGE | Rendered notebooks with casts to the spec types, MERGE on the business key, and a UC primary key |
| Silver: column order and audit columns | `_BK`, data, `InsertLoadStepID`, `InsertTimestamp`, `LastUpdateLoadStepID`, `LastUpdateTimestamp` |
| Silver: notebooks, one per table, shared UDFs, PEP 8, docstrings, markdown | One notebook per table; `databricks_de_common`; docstrings and markdown cells generated |
| Silver: VACUUM and OPTIMIZE | Weekly maintenance job |
| Gold: `_SK`, `_BK`, data, `StartDate`/`EndDate`/`IsCurrent`, audit | Rendered DDL and SCD2 MERGE |
| Gold: SQL MERGE in stored procedures, one per table | `<domain>.Load<Table>` Unity Catalog SQL procedure |
| Pull requests: one per notebook / procedure / story | One spec and its rendered files per pull request |

## Getting started

```bash
pip install -r requirements.txt            # toolkit, Databricks SDK and SQL connector
python -m pytest -q                        # offline tests
cd examples/insurance && python -m dbde check   # the worked example passes every rule
```

Then follow [`START_HERE.md`](START_HERE.md): run `/connect-workspace` (it asks for your workspace URL and SQL warehouse), then `/scaffold-databricks-project`, `/build-intermediate`, `/build-marts` and `/build-genie-space`.

## Project conventions

- Never commit hosts, ids, tokens or secrets. `dbde.yml` names CLI profiles and warehouses, not credentials.
- Never commit the Information Matrix workbook (it holds sampled data). `target/` is git-ignored.
- Never edit rendered files. Change the spec and run `python -m dbde render`. `python -m dbde check` fails on drift.
- `test` and `prod` are written only by the release pipeline.
