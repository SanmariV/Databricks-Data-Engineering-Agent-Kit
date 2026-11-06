---
applyTo: "models/**/*.yml,dbde.yml"
---

# Spec YAML files

- 2-space indents and lines of 80 characters or fewer. Use `>` for long descriptions.
- Every table description states the grain: "One row per policy. Grain = Policy_BK."
- Every column has a `description` with the business meaning, unit and allowed values. "The policy id" is not enough. Descriptions become Unity Catalog comments, which Genie reads.
- Tests:
  - column tests go under the column's `tests:`; table tests under the table's `tests:`
  - give parameters as a mapping: `- accepted_values: {values: [A, B]}`
  - use `severity: warn` only with a reason
- Never declare the columns the kit adds: `_SK`, `StartDate`, `EndDate`, `IsCurrent`, `InsertLoadStepID`, `InsertTimestamp`, `LastUpdateLoadStepID`, `LastUpdateTimestamp`.
- `dbde.yml` holds CLI profile names, SQL warehouse names, catalog names and schedules, never hosts, ids, tokens or secrets.
- After editing, run `python -m dbde render` and `python -m dbde check`.
