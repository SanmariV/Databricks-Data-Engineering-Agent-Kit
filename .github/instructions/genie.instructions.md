---
applyTo: "models/genie/**,models/gold/**,dbde.yml"
---

# AI enablement (metric views and Genie spaces)

The kit renders the AI-facing objects from the specs. You never write them by hand.

| Object | From | Rendered to |
| --- | --- | --- |
| Metric view `Metric<Process>` | a gold fact's `measures:` and `references:` | `gold/<domain>/metric_views/Metric<Process>.sql` |
| Genie space | `models/genie/<Name>.yml` | `genie/<Name>.geniespace.json`, `resources/genie_<name>.yml`, `gold/ops/grants/<Name>.sql` |

## Gold specs feed the metric view

- Every column and measure has a business description (meaning, unit, allowed values). It becomes the Unity Catalog comment and the metric view comment that Genie reads.
- A measure is an aggregate over the fact's columns (rule A1): `sum(...)`, `count(...)`, `count(distinct ...)`, `avg(...)`, `min(...)` or `max(...)`. A ratio is an expression over aggregates, e.g. `sum(AnnualPremiumAmount) / count(Policy_BK)`.
- The dimensions are the fact's own non-key, non-numeric columns and those of every dimension it references, minus PII columns. You don't list them.
- `synonyms:` on a table, column or measure say what people call things. `ai_instructions:` on a table say how to use it. Only write them when a person gave them. Otherwise leave them out and list them under "Decisions for you".

## A Genie space

```yaml
# models/genie/SalesAnalyst.yml
title: Sales Analyst
description: New business for sales managers, covering policies issued and premium.
instructions: |
  - Answer from sales.MetricNewBusiness with MEASURE(...).
  - Premium is annualised, in policy currency.
  - Never name or list individual people.
tables:
  - sales.MetricNewBusiness          # metric views and gold tables only
  - sales.FactNewBusiness
sample_questions:
  - How many new business policies did we issue last month?
examples:                            # three or more (rule A6)
  - question: What is the new business annual premium by branch?
    query: |
      select BranchCode, measure(AnnualPremiumTotal) as AnnualPremiumTotal
      from {{ ref('sales.MetricNewBusiness') }}
      group by all
benchmarks:                          # optional: question + expected SQL
  - question: How many new business policies were issued in total?
    query: |
      select measure(NewBusinessCount) as NewBusinessCount
      from {{ ref('sales.MetricNewBusiness') }}
permissions:
  can_run: [TODO(consumer group)]    # the real group; never invent one
```

- Every space has a title, a description (who it is for, what it answers) and instructions (what to answer, units, date conventions, what it must never do) (rule A3).
- `tables` lists gold tables and metric views only (rule A4). Prefer metric views: they carry the measure definitions and leave PII out.
- **Never list a table with PII columns** (rule A5). Genie cannot hide single columns. Use the fact's metric view instead, or ask for the PII to be split into its own table.
- Examples are read-only Databricks SQL over `{{ ref() }}` of the space's own tables (rule A4). Read a metric view with `measure(<Name>)` and `group by all`. Never name a PII column in instructions, questions or examples (rule A8).
- Run the examples with `python -m dbde test-genie --target dev`. A failing example is a finding. Report it; don't delete or weaken it.
- Deploy with `python -m dbde deploy --target dev`. It runs the gold SQL, then deploys the bundle with the space. Consumers get `CAN_RUN` on the space, and `SELECT` through `gold/ops/grants/<Name>.sql`.

## AI functions over text (not rendered yet)

The kit does not render `ai_query`, `ai_classify`, `ai_extract` or similar functions. If a project needs them, tell the user it needs a toolkit change (a new silver table kind in `dbde/render.py`), and apply these rules:

- keep the source key on every derived row (provenance)
- version prompts in the repo, and record the prompt version and model endpoint in the table `meta`
- run the function only in an incremental silver load, so each row is scored once
- declare the output with `accepted_values` on labels, and test that generated text is grounded in its source

Never hand-write the notebook.
