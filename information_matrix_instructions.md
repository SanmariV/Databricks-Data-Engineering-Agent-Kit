# Information Matrix: instructions

How to produce the Information Matrix workbook from this project, and what to watch for. The design is in section 11 of `databricks_engineering_standards.md`. The step-by-step agent prompt is `.github/prompts/information-matrix.prompt.md` (run it in Copilot Chat as `/information-matrix`).

## 1. One-off setup

1. Install the packages: `pip install -r requirements.txt`.
2. Make a blank template from the last filled matrix, if you don't have one:

   ```bash
   python scripts/build_information_matrix.py blank \
     --source "<filled matrix>.xlsx" \
     --out docs/information_matrix_template.xlsx
   ```

   The blank keeps the Usage Guide, Doc Controls layout, column headers and styles. It clears every data row and the version history. Check it for client locations (for example a SharePoint link on Doc Controls) before committing it to a shared repo.

3. For production, ask the platform team for the read-only **matrix service principal**: `USE CATALOG`, `USE SCHEMA` and `SELECT` on the silver and gold catalogs, and `CAN USE` on a SQL warehouse, nothing else. Never profile production as yourself.

## 2. Where the content comes from

| Sheet | Source in this repo |
| --- | --- |
| EntitiesDimensions, Attributes | `Dim` tables: spec descriptions, columns, `meta.matrix` |
| Measure Groups | `Fact` tables: description (with `Grain = ...`), `meta.matrix` |
| Measures | `measures:` on facts: `name`, `expression`, `description`, `meta.matrix` |
| BUS Matrix-current | `references` columns on facts (the implied `relationships` tests) |
| SilverLayerMapping | Silver specs and their lineage to bronze columns |
| Source Table / Source Columns | Spec lineage: silver expressions, explicit `lineage:` entries, or same-named columns in the tables a model reads |
| Data Example, Cardinality, BUS Matrix check | The `profile` step (read-only queries; PII never sampled) |
| Glossary | `reference/business_glossary.csv` |

## 3. Moving from a hand-written matrix

Rows are matched across versions **by name**. A row that keeps its name keeps everything people typed into it. A row whose name changes shows twice: the old one as "Removed" and the new one as "Added".

- Entities and measure groups: set `meta.matrix.name` on the table to the old name if it differs.
- Attributes: column names cannot be aliased. Copy old comments into the column's `meta.matrix.comments`.
- Measures: name the measure as in the old matrix, or copy the notes into its `meta.matrix`.

Review every "Removed" row before publishing the first generated version.

## 4. Each new version

```bash
python -m dbde parse --target prod
python scripts/build_information_matrix.py profile --target prod --out target/matrix_profile.json   # matrix service principal only
python scripts/build_information_matrix.py build \
  --manifest target/manifest.json --profile target/matrix_profile.json \
  --template docs/information_matrix_template.xlsx \
  --previous "<last published matrix>.xlsx" \
  --version "1.2 Production" --author "<your name>" \
  --out "target/Information Matrix 1.2 Production.xlsx"
```

Then:

1. Read `target/Information Matrix <version>.gaps.md`.
2. Fix gaps in the specs (`meta.matrix`, descriptions, measures), never in the workbook. Then build again.
3. Save the workbook to the client document location. Never commit it.

## 5. What is not generated

- The legacy `BUS Matrix` sheet. `BUS Matrix-current` replaces it.
- Working sheets in the template. They are left exactly as they are.
- Excel dropdowns that point at other sheets. openpyxl drops them when it saves. In-sheet list dropdowns survive.
