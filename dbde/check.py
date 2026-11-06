"""Project checks: layer rules, spec completeness, naming and data types.

This replaces dbt's parser errors, ``check_layers.py`` and
``dbt_project_evaluator``. Every finding names its rule; the rules are
listed in ``databricks_engineering_standards.md`` (section 12) with the
reason. ``error`` findings fail ``python -m dbde check``; ``warn`` findings
are reported and need a decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from dbde import dq, genie
from dbde.project import (AUDIT_COLUMNS, PROCEDURE_NAMES, REF_PATTERN,
                          SCD_COLUMNS, Model, Project, identifiers, rel)

TYPES = re.compile(
    r"^(string|varchar\(\d+\)|char\(\d+\)|boolean|tinyint|smallint|int|"
    r"bigint|float|double|date|timestamp|timestamp_ntz|binary|"
    r"decimal\(\d+,\s*\d+\))$")
NOT_DATABRICKS = {
    "nvarchar": "string", "nchar": "string", "ntext": "string",
    "text": "string", "datetime": "timestamp", "datetime2": "timestamp",
    "smalldatetime": "timestamp", "datetimeoffset": "timestamp",
    "money": "decimal(18,2)", "smallmoney": "decimal(18,2)",
    "bit": "boolean", "uniqueidentifier": "string", "image": "binary",
    "varbinary": "binary", "xml": "string", "numeric": "decimal(p,s)",
    "integer": "int", "long": "bigint", "real": "float",
}
PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
COLUMN = re.compile(r"^[A-Z][A-Za-z0-9]*(_BK|_SK)?$")
SCHEMA = re.compile(r"^[a-z][a-z0-9_]*$")
ABBREVIATIONS = {
    "Amt": "Amount", "Qty": "Quantity", "Cust": "Customer",
    "Desc": "Description", "Nbr": "Number", "Num": "Number", "No": "Number",
    "Addr": "Address", "Dt": "Date", "Cd": "Code", "Acct": "Account",
    "Prem": "Premium", "Txn": "Transaction", "Trx": "Transaction",
    "Cnt": "Count", "Pct": "Percentage", "Dept": "Department",
    "Mgr": "Manager", "Prod": "Product", "Dob": "BirthDate",
    "Val": "Value", "Tel": "Telephone", "Src": "Source", "Tgt": "Target",
    "Tmp": "Temporary", "Misc": "Miscellaneous", "Flg": "Flag",
    "Ind": "Indicator", "Sts": "Status", "Stat": "Status", "Typ": "Type",
}
PII_HINT = re.compile(
    r"(First|Last|Middle|Full|Sur|Given|Family|Customer|Client|Member|"
    r"Policyholder|Person|Contact|Employee|Agent)Name|Surname|Email|Phone|"
    r"Mobile|Address|PostalCode|ZipCode|Birth|IdNumber|NationalId|Passport|"
    r"TaxNumber|Gender|^Age$|AgeYears", re.I)
AGGREGATE = re.compile(
    r"\b(sum|count|avg|mean|min|max|median|percentile|percentile_approx|"
    r"approx_count_distinct|stddev|variance|count_if|any_value|try_avg|"
    r"try_sum)\s*\(", re.I)
RESERVED = set(AUDIT_COLUMNS) | set(SCD_COLUMNS) | set(PROCEDURE_NAMES)
MIN_EXAMPLES = 3


@dataclass
class Finding:
    """One check result.

    Attributes:
        rule: Rule id, e.g. ``L1``.
        severity: ``error`` or ``warn``.
        file: Spec or SQL file (relative path).
        message: What is wrong and how to fix it.
    """

    rule: str
    severity: str
    file: str
    message: str


def _words(name: str) -> List[str]:
    """Split a PascalCase name into words.

    Args:
        name: e.g. ``PolicyStatusCode_BK``.

    Returns:
        e.g. ``['Policy', 'Status', 'Code']``.
    """
    base = re.sub(r"_(BK|SK)$", "", name)
    return re.findall(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])", base)


def _strip_comments(sql: str) -> str:
    """Return SQL without ``--`` and ``/* */`` comments."""
    return re.sub(r"/\*.*?\*/", " ", re.sub(r"--[^\n]*", "", sql), flags=re.S)


def run_checks(project: Project) -> List[Finding]:
    """Run every check on the project.

    Args:
        project: The loaded project.

    Returns:
        Findings, errors first.
    """
    root = project.config.root
    out: List[Finding] = [Finding("P0", "error", f, m)
                          for f, m in project.errors]
    catalogs = sorted({c for t in project.config.targets
                       for c in project.config.catalogs(t).values()})
    hard_coded = re.compile(
        r"\b(" + "|".join(re.escape(c) for c in catalogs) + r")\s*\.", re.I) \
        if catalogs else re.compile(r"(?!)")

    def add(rule: str, severity: str, path, message: str) -> None:
        out.append(Finding(rule, severity, rel(path, root), message))

    for name, settings in project.config.targets.items():
        for layer, catalog in project.config.catalogs(name).items():
            if not SCHEMA.match(catalog):
                add("P2", "error", root / "dbde.yml",
                    f"target {name}: {layer} catalog '{catalog}' must be "
                    "lower snake_case")
        warehouse = str(settings.get("warehouse") or "")
        if not warehouse or warehouse.upper().startswith("TODO"):
            add("P2", "warn", root / "dbde.yml",
                f"target {name}: no warehouse (the SQL warehouse name the "
                "toolkit and Genie use)")

    # Sources --------------------------------------------------------------
    for source in project.sources.values():
        if not source.description:
            add("S1", "error", source.path,
                f"source {source.name} has no description")
        if not source.freshness.get("loaded_at_column"):
            add("S7", "warn", source.path,
                f"source {source.name} has no freshness rule "
                "(loaded_at_column, warn_after_hours, error_after_hours)")
        for table in source.tables.values():
            if not table.description:
                add("S1", "warn", source.path,
                    f"{source.name}.{table.name}: no description")
            if not table.columns:
                add("S8", "error", source.path,
                    f"{source.name}.{table.name}: no columns; run "
                    f"python -m dbde generate-source {source.name}")

    # Bronze tables read by more than one silver table (1:1 rule) ---------
    readers: dict = {}
    for model in project.silver():
        if model.source:
            readers.setdefault(model.source, []).append(model.key)
    for (source, table), keys in readers.items():
        if len(keys) > 1:
            add("L6", "error", project.models[keys[0]].path,
                f"bronze {source}.{table} feeds {', '.join(keys)}; one "
                "silver source table per bronze table")
    for source in project.sources.values():
        for table in source.tables:
            if (source.name, table) not in readers:
                add("L7", "warn", source.path,
                    f"bronze {source.name}.{table} has no silver table yet")

    silver_schemas = {m.schema for m in project.silver() if m.kind == "source"}
    integrated_schemas = {m.schema for m in project.silver()
                          if m.kind == "integrated"}
    for schema in silver_schemas & integrated_schemas:
        add("N5", "error", root / "models" / "silver" / schema,
            f"schema '{schema}' holds both source and integrated silver "
            "tables; use the source name for one and a domain for the other")
    if "ops" in {m.schema for m in project.gold()}:
        add("N5", "error", root / "models" / "gold" / "ops",
            "the gold schema 'ops' is reserved for the kit's data quality "
            "objects")

    if project.config.layer_schemas.get("gold") == "ops":
        add("N5", "error", root / "dbde.yml",
            "layer_schemas.gold cannot be 'ops'; it is reserved for the "
            "kit's data quality objects")
    created: dict = {}
    for model in project.silver() + project.gold():
        created.setdefault((model.layer, model.table.lower()),
                           []).append(model)
    for (layer, table), models in created.items():
        if len(models) > 1:
            add("N5", "error", models[1].path,
                f"{', '.join(m.key for m in models)} would all be created "
                f"as {layer} table {models[0].table} (layer_schemas in "
                "dbde.yml); give them different table names")

    gold_names: dict = {}
    for model in project.gold():
        gold_names.setdefault(model.name, []).append(model.key)
    for name, keys in gold_names.items():
        if len(keys) > 1:
            add("G5", "error", project.models[keys[0]].path,
                f"{name} exists in several schemas ({', '.join(keys)}); one "
                "concept, one gold table")

    for model in project.silver() + project.gold():
        out.extend(_model_checks(project, model, hard_coded))

    # Tests -------------------------------------------------------------
    for test in dq.all_tests(project):
        model = project.models.get(test.table)
        path = model.path if model else root / "models"
        if test.severity not in dq.SEVERITIES:
            add("T1", "error", path, f"{test.table} {test.name}: severity "
                f"must be error or warn, not '{test.severity}'")
        allowed = dq.COLUMN_TESTS if test.column else dq.TABLE_TESTS
        if not test.implied and test.kind not in allowed:
            add("T1", "error", path,
                f"{test.table}: unknown {'column' if test.column else 'table'}"
                f" test '{test.kind}'. Allowed: {', '.join(sorted(allowed))}")
            continue
        if test.kind == "accepted_values" and not test.args.get("values"):
            add("T2", "error", path, f"{test.table} {test.name}: "
                "accepted_values needs a non-empty values list")
        if test.kind in ("relationships", "reconciles_to"):
            other = str(test.args.get("to") or test.args.get("compare_to"))
            if other not in project.models:
                add("T3", "error", path, f"{test.table} {test.name}: "
                    f"'{other}' is not a model (use schema.Table)")
        if test.kind == "expression" and not (test.args.get("expression")
                                              or test.args.get("value")):
            add("T2", "error", path, f"{test.table} {test.name}: "
                "expression test needs an expression")
    out.extend(_genie_checks(project))
    order = {"error": 0, "warn": 1}
    return sorted(out, key=lambda f: (order.get(f.severity, 2), f.file,
                                      f.rule))


def _pii_names(project: Project) -> List[str]:
    """Return the names of every column marked PII, in any layer."""
    return sorted({c.name for m in project.models.values()
                   for c in m.columns if c.pii})


def _genie_checks(project: Project) -> List[Finding]:
    """Check the metric views and Genie spaces are built from good specs.

    Args:
        project: The loaded project.

    Returns:
        Findings (rules A1-A9).
    """
    root = project.config.root
    out: List[Finding] = []

    def add(rule: str, severity: str, path, message: str) -> None:
        out.append(Finding(rule, severity, rel(path, root), message))

    gold = project.gold()
    for model in project.silver() + gold:
        pairs = [(model.key, model.spec.get("synonyms"))]
        pairs += [(f"{model.key}.{c.name}", c.synonyms)
                  for c in model.columns]
        pairs += [(f"{model.key}.{m.get('name')}", m.get("synonyms"))
                  for m in model.measures]
        for label, value in pairs:
            if value not in (None, [], "") and (
                    not isinstance(value, list)
                    or not all(isinstance(v, str) and v.strip()
                               for v in value)):
                add("A2", "error", model.path, f"{label}: synonyms must be "
                    "a list of words, e.g. synonyms: ['Premium', 'GWP']")
        if model.spec.get("ai_instructions") not in (None, "") and \
                not model.ai_instructions:
            add("A2", "error", model.path, f"{model.key}: ai_instructions "
                "must be text")
        if not model.measures:
            continue
        pii = {c.name.lower() for c in model.columns if c.pii}
        dims = set()
        if model.kind == "fact":
            dims = {d["name"] for d in
                    genie.metric_view_dimensions(project, model)}
        for measure in model.measures:
            expression = str(measure.get("expression") or "")
            name = str(measure.get("name") or "")
            if expression and not AGGREGATE.search(expression):
                add("A1", "error", model.path, f"{model.key}: measure {name} "
                    "must aggregate (sum, count, avg, min, max, ...); a "
                    "metric view measure is an aggregate expression")
            used = set(identifiers(expression)) & pii
            if used:
                add("A5", "error", model.path, f"{model.key}: measure {name} "
                    f"reads personal data ({', '.join(sorted(used))})")
            if name in dims:
                add("A1", "error", model.path, f"{model.key}: measure {name} "
                    "has the name of a metric view dimension; rename it")
    if gold and not any(m.measures for m in gold):
        add("A7", "warn", root / "models" / "gold",
            "no fact declares measures; no metric view is rendered and "
            "Genie can only list rows, not answer 'how much'")

    views = project.metric_views
    pii_names = _pii_names(project)
    for space in project.spaces.values():
        def fail(rule: str, message: str, severity: str = "error") -> None:
            add(rule, severity, space.path, f"{space.name}: {message}")

        if not space.title:
            fail("A3", "no title")
        if not space.description:
            fail("A3", "no description (who it is for, what it answers)")
        if not space.instructions:
            fail("A3", "no instructions (what to answer, units, what it "
                 "must never do)")
        if not space.tables:
            fail("A3", "no tables; list gold tables and metric views")
        for key in space.tables:
            model = project.models.get(key)
            if key in views:
                continue
            if not model or model.layer != "gold":
                fail("A4", f"'{key}' is not a gold table or metric view "
                     "(use schema.Table, e.g. sales.MetricNewBusiness)")
            elif any(c.pii for c in model.columns):
                names = ", ".join(c.name for c in model.columns if c.pii)
                fail("A5", f"{key} has personal data ({names}) and Genie "
                     "cannot hide single columns. Remove it and use the "
                     "metric view (it leaves PII out), or split the PII "
                     "into its own table")
        if len(space.examples) < MIN_EXAMPLES:
            fail("A6", f"{len(space.examples)} example question(s); give "
                 f"{MIN_EXAMPLES} or more, covering the questions people ask "
                 "most")
        if not space.groups or any(g.upper().startswith("TODO")
                                   for g in space.groups):
            fail("A9", "set permissions.can_run to the real consumer "
                 "group(s); the bundle deploy fails on a TODO group", "warn")
        for example in space.examples + space.benchmarks:
            question = str(example.get("question") or "").strip()
            query = str(example.get("query") or "")
            if not question or not query.strip():
                fail("A4", "every example needs a question and a query")
                continue
            refs = [m.group("key") for m in REF_PATTERN.finditer(query)]
            for key in refs:
                if key not in project.models and key not in views:
                    fail("A4", f"'{question}': ref('{key}') is not a model "
                         "or metric view")
                elif key not in space.tables:
                    fail("A4", f"'{question}': reads {key}, which is not one "
                         "of the space's tables")
            if not refs:
                fail("A4", f"'{question}': read tables with "
                     "{{ ref('schema.Table') }}")
            sql = genie.example_sql(project, example)
            try:
                from dbde import connect
                connect.assert_read_only(sql)
            except Exception as exc:
                fail("A4", f"'{question}': {exc}")
            else:
                try:
                    import sqlglot
                    sqlglot.parse_one(sql.replace("${", "").replace(
                        "_catalog}", "_catalog"), read="databricks")
                except ImportError:
                    pass
                except Exception as exc:
                    fail("A4", f"'{question}': not valid Databricks SQL: "
                         f"{str(exc).splitlines()[0]}")
        text = " ".join([space.instructions, space.description]
                        + space.sample_questions
                        + [str(e.get("question") or "") + " "
                           + str(e.get("query") or "")
                           for e in space.examples + space.benchmarks])
        named = [c for c in pii_names if re.search(rf"\b{c}\b", text)]
        if named:
            fail("A8", "personal data columns named in instructions, "
                 f"questions or examples: {', '.join(named)}")
    return out


def _model_checks(project: Project, model: Model,
                  hard_coded: re.Pattern) -> List[Finding]:
    """Run the checks for one silver or gold model.

    Args:
        project: The loaded project.
        model: The model.
        hard_coded: Pattern matching hard-coded catalog names.

    Returns:
        Findings for this model.
    """
    root = project.config.root
    out: List[Finding] = []
    sql_file = model.path.with_suffix(".sql")

    def add(rule: str, severity: str, message: str, path=None) -> None:
        out.append(Finding(rule, severity, rel(path or model.path, root),
                           f"{model.key}: {message}"))

    # Structure -------------------------------------------------------------
    if not SCHEMA.match(model.schema):
        add("N4", "error", "schema (folder) names are lower snake_case")
    if not PASCAL.match(model.name):
        add("N1", "error", "table names are PascalCase, singular")
    if not model.description:
        add("S1", "error", "no description")
    elif not re.search(r"grain\s*=", model.description, re.I):
        add("S2", "error", "description must state the grain "
            "('Grain = <key columns>')")
    if not model.columns:
        add("S3", "error", "no columns")
    if not model.business_keys:
        add("S4", "error", "no business key; mark the key column(s) "
            "business_key: true (DE Policy v2: every table has a logical "
            "primary key)")

    if model.layer == "silver":
        if re.match(r"^(Dim|Fact|Bridge|Metric)[A-Z]", model.name):
            add("N2", "error", "Dim/Fact/Bridge/Metric prefixes are for gold "
                "only")
        if model.kind == "source" and not model.source:
            add("L1", "error", "source must be {source: <source>, table: "
                "<bronze table>}")
        elif model.kind == "source":
            source, table = model.source
            bronze = project.sources.get(source)
            if not bronze or table not in bronze.tables:
                add("L1", "error", f"source {source}.{table} is not declared "
                    "in models/sources/")
            else:
                known = {c.lower() for c in bronze.tables[table].columns}
                for col in model.columns:
                    expr = col.expression or col.name
                    tokens = [t for t in identifiers(str(expr))
                              if t in known]
                    if not col.expression:
                        add("S5", "error", f"{col.name}: no expression "
                            "(the bronze column or Spark SQL it comes from)")
                    elif not tokens:
                        add("S5", "warn", f"{col.name}: expression uses no "
                            "bronze column; check the column names")
            if model.sql:
                add("L2", "error", "a silver source table is built from its "
                    "spec only; delete the .sql file", sql_file)
            if model.load.get("mode") == "incremental" and \
                    not model.load.get("column"):
                add("S6", "error", "incremental load needs load.column")
        else:
            if not model.sql:
                add("S9", "error", "integrated silver table needs a .sql "
                    "SELECT next to its spec", sql_file)
            for ref in model.refs:
                target = project.models.get(ref)
                if target and target.layer != "silver" or \
                        ref in project.metric_views:
                    add("L3", "error", f"reads {ref}; silver never reads "
                        "gold", sql_file)
    else:
        if model.kind == "unknown":
            add("N2", "error", "gold table names start with Dim, Fact or "
                "Bridge")
        if model.kind == "dimension" and model.scd_type not in (1, 2):
            add("G1", "error", "dimensions set scd_type: 1 or 2")
        if model.kind != "dimension" and model.scd_type:
            add("G1", "warn", "scd_type only applies to dimensions")
        if model.measures and model.kind != "fact":
            add("G4", "error", "measures belong on facts")
        if not model.sql:
            add("S9", "error", "gold table needs a .sql SELECT next to its "
                "spec", sql_file)
        body = _strip_comments(model.sql or "")
        if body and re.search(r"(?<!:):(?!:)[A-Za-z_]\w*|\$\{", body):
            add("G6", "error", "the SELECT runs inside the load procedure "
                "and cannot use :parameters or ${...}; use load.mode "
                "incremental for date windows", sql_file)
        used = {t for t in identifiers(body)} & {n.lower()
                                                 for n in PROCEDURE_NAMES}
        if used:
            add("G6", "error", f"the SELECT uses {', '.join(sorted(used))}, "
                "a name the load procedure reserves", sql_file)
        if body and re.search(r"\border\s+by\b", body, re.I) and \
                not re.search(r"\bover\s*\(", body, re.I):
            add("G7", "warn", "ORDER BY in the MERGE source is ignored; "
                "remove it", sql_file)
        if body and re.search(r"\blimit\s+\d", body, re.I):
            add("G7", "error", "LIMIT drops rows from a gold load; remove "
                "it", sql_file)
        for ref in model.refs:
            target = project.models.get(ref)
            if target and target.layer == "gold" and target.kind == "fact" \
                    and model.kind == "dimension":
                add("L4", "warn", f"dimension reads fact {ref}; check the "
                    "design", sql_file)
            if ref in project.metric_views:
                add("L3", "error", f"reads metric view {ref}; gold tables "
                    "read tables, not metric views", sql_file)
        if model.load.get("mode") == "incremental" and \
                not model.load.get("column"):
            add("S6", "error", "incremental load needs load.column")

    # SQL references -------------------------------------------------------
    if model.sql:
        body = _strip_comments(model.sql)
        if hard_coded.search(body):
            add("L5", "error", "hard-coded catalog name; use "
                "{{ ref('schema.Table') }}", sql_file)
        if "{{ source(" in body.replace("{{source(", "{{ source("):
            add("L5", "error", "source() is only used by silver source "
                "tables (in the spec); read silver with ref()", sql_file)
        for match in REF_PATTERN.finditer(body):
            key = match.group("key")
            if key not in project.models and key not in project.metric_views:
                add("L8", "error", f"ref('{key}') is not a model", sql_file)
        if model.key in model.refs:
            add("L8", "error", "the table reads itself", sql_file)
        if re.search(r"\b(current_timestamp|current_date|now|getdate|"
                     r"sysdatetime|rand|random|uuid|newid|"
                     r"monotonically_increasing_id)\s*\(|"
                     r"\bcurrent_(timestamp|date)\b", body, re.I):
            add("I1", "error", "non-deterministic function in the logic; "
                "silver uses :load_date, gold uses load.mode incremental",
                sql_file)

    # Columns -------------------------------------------------------------
    seen = set()
    for col in model.columns:
        if not col.name:
            add("S3", "error", "a column has no name")
            continue
        if col.name.lower() in seen:
            add("S3", "error", f"{col.name}: declared twice")
        seen.add(col.name.lower())
        if col.name in RESERVED or col.name == model.surrogate_key:
            add("S10", "error", f"{col.name}: added or reserved by the kit; "
                "rename or remove it")
            continue
        if not col.description:
            add("S1", "error", f"{col.name}: no description")
        elif str(col.description).strip().upper().startswith("TODO"):
            add("S1", "warn", f"{col.name}: {col.description}")
        dtype = col.data_type
        base = dtype.split("(")[0]
        if not dtype:
            add("D1", "error", f"{col.name}: no data_type")
        elif base in NOT_DATABRICKS:
            add("D2", "error", f"{col.name}: {dtype} is not a Databricks "
                f"type; use {NOT_DATABRICKS[base]}")
        elif not TYPES.match(dtype):
            add("D1", "error", f"{col.name}: '{dtype}' is not an allowed "
                "type (string, varchar(n), boolean, int, bigint, "
                "decimal(p,s), date, timestamp, ...)")
        elif base in ("float", "double") and re.search(
                r"Amount|Amt|Premium|Prem|Price|Cost|Balance|Value|Total",
                col.name):
            add("D3", "error", f"{col.name}: money is decimal(18,2), never "
                "a floating type")
        if not COLUMN.match(col.name):
            add("N3", "error", f"{col.name}: column names are PascalCase "
                "(suffix _BK for business keys, _SK for surrogate keys)")
        if col.business_key and not col.name.endswith("_BK"):
            add("N6", "error", f"{col.name}: business key columns end "
                "with _BK")
        # A non-key column may end in _BK: it carries another entity's
        # business key (e.g. Agent_BK on Policy), named as in its own table.
        if col.name.endswith("_SK"):
            if model.layer == "silver":
                add("N7", "error", f"{col.name}: surrogate keys are gold only")
            elif not col.references:
                add("G2", "error", f"{col.name}: _SK column needs "
                    "references: <schema>.<DimTable>")
        if col.references:
            ref = project.models.get(col.references)
            if not ref or ref.kind != "dimension":
                add("G2", "error", f"{col.name}: references "
                    f"'{col.references}', which is not a gold dimension")
            elif not col.name.endswith(f"{ref.name}_SK"):
                add("G3", "error", f"{col.name}: a key to {ref.name} is "
                    f"named <Role>{ref.name}_SK")
            if col.data_type != "bigint":
                add("G3", "error", f"{col.name}: surrogate keys are bigint")
        for word in _words(col.name):
            if word in ABBREVIATIONS:
                add("N8", "warn", f"{col.name}: '{word}' is an abbreviation; "
                    f"use '{ABBREVIATIONS[word]}'")
        is_bool = base == "boolean"
        if is_bool and not re.match(r"^(Is|Has)[A-Z]", col.name):
            add("N9", "error", f"{col.name}: boolean columns start with Is "
                "or Has")
        if re.match(r"^(Is|Has)[A-Z]", col.name) and not is_bool:
            add("N9", "error", f"{col.name}: Is/Has columns are boolean")
        if col.name.endswith("Date") and base != "date":
            add("N10", "error", f"{col.name}: ...Date columns are date")
        if re.search(r"(Timestamp|DateTime)$", col.name) and base not in (
                "timestamp", "timestamp_ntz"):
            add("N10", "error", f"{col.name}: ...Timestamp columns are "
                "timestamp (UTC)")
        if "pii" not in col.meta and PII_HINT.search(col.name):
            add("P1", "warn", f"{col.name}: looks like personal data; set "
                "meta.pii: true (or false) in the spec")
    for measure in model.measures:
        if not measure.get("name") or not measure.get("expression"):
            add("G4", "error", "each measure needs a name and an expression")
        elif not measure.get("description"):
            add("G4", "error", f"measure {measure['name']}: no description "
                "(it becomes the measure comment Genie reads)")
        elif not PASCAL.match(str(measure["name"])):
            add("N3", "error", f"measure {measure['name']}: PascalCase")
    return out
