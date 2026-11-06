---
agent: agent
description: Propose the integrated silver tables (the intermediate layer) from the silver source tables, then build the ones the user confirms.
---

# Build the integrated silver layer

Domain folder: **${input:domain:business domain, snake_case, e.g. sales}**

Follow `AGENTS.md` and `.github/instructions/silver.instructions.md`. Business logic needs a person: never guess a join key or a business rule.

## Phase 1: Propose (change no files)

1. Read every `models/silver/*/*.yml` source table spec.
2. Before you list an open question, check whether the data can answer it: one read-only `python -m dbde show "..." --target dev` query with `union all`, counts only, nothing from PII columns. Put the evidence in the question.
3. Print this, and nothing else:

```
What the data looks like
- <schema.Table>: one row per <grain> (key: <_BK columns>). Carries <measures and attributes, in one line>.
- (one line per silver table, plus how they can join)

Suggested integrated tables (models/silver/<domain>/)
1. <Table>: <grain>. Inputs: <schema.Table, ...>. <what it does>.
   Answered from data: <finding with the numbers, if you checked>.
   Open question: <only what the data can't answer> Recommend: <your answer>.
2. ...
(2 to 4 tables; mark any that depend on an undecided business rule as Optional)

Gold tables these would feed
- Dim..., Fact...

Your next step: reply "build 1 and 2" (or the numbers you want), or change the list.
```

4. **Stop.** Don't create any file until the user replies.

## Phase 2: Build (only the tables the user confirmed)

For each confirmed table:

1. Write `models/silver/<domain>/<Table>.sql`: a Spark SQL SELECT with import CTEs, one-job CTEs and a final `select`. Read only `{{ ref('schema.Table') }}` silver tables. Return exactly the spec's columns.
2. Write `models/silver/<domain>/<Table>.yml`:
   - a description stating the grain, and a description on every column
   - `business_key: true` on the grain column(s), with a `_BK` name
   - Spark data types
   - tests for codes, ranges and row rules
   - `meta.pii` carried over from the inputs
3. Use a recommended answer only if the user accepted it. Otherwise write `TODO(...)` in the description and list it.

Then run, and fix any failure before you report:

```bash
python -m dbde render
python -m dbde check
python -m dbde build --target dev --layer silver --skip-source-tests
```

A failing test is a data quality finding: report it with the counts. Don't delete or weaken the test.

## Finish

End with the standard block from `AGENTS.md`, "How to finish every task". Put "run `/build-marts` for <domain>" as one of the next steps.
