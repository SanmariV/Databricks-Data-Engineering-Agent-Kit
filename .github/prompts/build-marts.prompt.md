---
agent: agent
description: Plan the gold tables (Dim/Fact/Bridge) from silver, then build the ones the user confirms.
---

# Build the gold tables

Domain folder: **${input:domain:business domain, snake_case, e.g. sales}**

Follow `AGENTS.md` and `.github/instructions/gold.instructions.md`. Business rules need a person: never guess a key, a measure definition or a dimension rule.

## Phase 1: Plan (change no files)

1. Read the silver specs, any `docs/specs/<domain>/*.yml` plans from the scaffold, and the existing gold specs.
2. Before you list an open question, check whether the data can answer it (`AGENTS.md`, "Check the data before you ask the user"). Use one read-only `python -m dbde show` query, counts only, nothing from PII columns. Check:
   - that the dimension key is unique
   - whether attributes change over time (this decides SCD type 1 or 2)
   - how many fact rows find their dimension row
3. Print this, and nothing else:

```
Gold tables planned for <domain>

1. Dim<Entity>: grain <grain>, SCD type <1/2>. Reads: <silver tables>. Attributes: <list>.
   Answered from data: <finding with numbers>.
   Open question: <only what the data can't answer> Recommend: <answer>.
2. Fact<Process>: grain <grain>. Reads: <tables>. Dimensions: <Dim..._SK, ...>. Measures: <list with definitions>.
   (If an integrated silver table is missing, say which one is needed first.)
...

Personal data: <columns you would mark pii: true>. Sensitive, your decision: <columns, with a recommendation>.

Your next step: reply "build 1 and 2" (or the numbers you want), or change the list.
```

4. **Stop.** Don't create any file until the user replies.

## Phase 2: Build (only what the user confirmed)

Build dimensions before the facts that reference them. For each confirmed table:

1. Write `models/gold/<domain>/<Table>.sql`: a Databricks SQL SELECT with import CTEs, one-job CTEs and a final `select`.
   - Read only `{{ ref() }}`.
   - No `:parameters`, `${...}`, `order by`, `limit` or `current_date()`; none of the procedure names (`RunLoadDate`, `MaxKey`, ...).
   - Resolve dimension keys with a point-in-time join and `coalesce(<Dim>.<Dim>_SK, -1)`.
2. Write `models/gold/<domain>/<Table>.yml`:
   - a description with `Grain = ...`, and a description on every column
   - Databricks data types
   - `business_key: true` on the grain
   - `scd_type` (dimensions)
   - `references` on every `_SK` column
   - `load: {mode: incremental, ...}` for large facts
   - `measures` with descriptions (facts)
   - business tests, and `reconciles_to` for financial facts when a control total exists (otherwise list it)
3. Mark PII columns `meta.pii: true` in this table and in the silver specs they come from. Don't mark sensitive amounts yourself; list them under "Decisions for you".
4. Add `meta.matrix` facts the user or the matrix gave you. Never invent them.
5. Unconfirmed rules become `TODO(...)` and are listed.

Then run, and fix any failure before you report:

```bash
python -m dbde render
python -m dbde check
python -m dbde build --target dev --layer gold
```

A failing test is a data quality finding: report it with the counts (for example, fact rows pointing at the unknown member `-1`). Don't delete or weaken the test.

## Finish

End with the standard block from `AGENTS.md`, "How to finish every task". Put "run `/project-check`" and "run `/information-matrix`" among the next steps.
