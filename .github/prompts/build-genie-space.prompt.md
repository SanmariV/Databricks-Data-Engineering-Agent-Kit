---
agent: agent
description: Make the gold layer AI-ready (metric views and a Genie space). Propose first, build what the user confirms.
---

# Build a Genie space

Space name: **${input:space:PascalCase name, e.g. SalesAnalyst}**

Consumer group: **${input:group:the workspace or account group that may use the space, e.g. sales-analysts, or "later"}**

If the group input is empty, ask once in chat:

```
Which group should be able to use this Genie space? Give the exact group name
from Databricks (Settings → Identity and access → Groups), or say "later".
They get CAN_RUN on the space and SELECT on the tables it reads.
```

Never invent a group. If the user says "later", write `TODO(consumer group)`. Then make "set the consumer group" the first next step, because the bundle deploy of the space fails until it is set.

Follow `AGENTS.md` and `.github/instructions/genie.instructions.md`. Synonyms, instructions, consumer groups and the meaning of a measure are facts people know: never invent them.

Gold must build and its tests must pass first. If not, say so and stop.

## Phase 1: Plan (change no files)

1. Read the gold specs, `dbde.yml` and any `models/genie/*.yml`. Run `python -m dbde render --check` to see what is already rendered.
2. For each fact, list the numeric columns that are not keys. A measure candidate is a sum or count of one of them, or an average or ratio for rates. Never sum a rate or a balance. Check anything unclear with one read-only `python -m dbde show` query (counts only, nothing from PII columns).
3. Check the AI readiness of every gold table the space would read:
   - a description on the table and every column, with unit and allowed values
   - at least one measure on each fact, each with a description (it becomes the metric view)
   - PII columns marked `meta.pii: true` (they stay out of metric views)
   - tables that hold PII: Genie cannot hide a column, so the space reads the metric view instead
4. Print this, and nothing else:

```
Genie space planned: <Name>

Purpose: <who it is for, what it answers, in one line>
Tables: <metric views and gold tables, none with PII>.
Left out because of PII: <tables/columns, or none>.

Measures to add:
1. <Fact>.<MeasureName> = <expression>. Definition: <text>. Source of the definition: <column description / your wording>.
   (Where you worded it yourself, say "draft, confirm with the business".)

Example questions to cover (3 to 6, in the words people use):
1. <question>

Open questions (only what the data can't answer):
1. <question>. Recommend: <answer>.

Your next step: reply "build all" (or the numbers you want), or change the list.
```

5. **Stop.** Don't create any file until the user replies.

## Phase 2: Build (only what the user confirmed)

1. Edit the gold specs: add `measures:` (aggregates, with `description`), and the `synonyms:` and `ai_instructions:` the user confirmed. Mark draft definitions with `TODO(confirm with the business)` in the description and list them.
2. Write `models/genie/<Name>.yml`:
   - `title`, `description` and `instructions` (units, date conventions, what it must never do)
   - `tables`: metric views (`<domain>.Metric<Process>`) and gold tables without PII
   - `sample_questions`
   - three or more `examples` as Databricks SQL over `{{ ref() }}` of the space's tables; metric views use `measure(<Name>)` and `group by all`
   - `benchmarks` the user gave (question and expected SQL)
   - `permissions.can_run`: the consumer group the user gave, or `TODO(consumer group)` if they said "later"
3. Run `python -m dbde render`, then `python -m dbde check`. Rules A1 to A8 must pass. A9 warns until the real group is set.
4. With a dev workspace, run `python -m dbde deploy --target dev`, then `python -m dbde test-genie --target dev`. Report each failing example with its error. Don't weaken or delete an example to make it pass.
5. Tell the user what only a person can do in Databricks:
   - open the space and ask it the example questions
   - run the benchmarks in the Genie UI and compare the answers
   - set the real consumer group, if it is still `TODO`
   - share the space

Report the tables and columns you kept out of the space because of PII, so the user can correct them. End with the finish block from `AGENTS.md`.
