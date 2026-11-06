"""Data quality tests: what to test and the Databricks SQL that tests it.

Every test is a query that returns the failing rows; zero rows is a pass.
The same SQL runs in two places:

* ``python -m dbde test`` runs it on a SQL warehouse and prints the results;
* ``ops.RunDataQualityTests`` (a stored procedure rendered by
  ``dbde.render``) runs it in the daily job, logs counts to
  ``ops.DataQualityResult`` and fails the job when an ``error`` test fails.

Tests come from the specs (``tests:`` on columns and tables) plus tests
every table gets without asking (implied tests, see ``implied_tests``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dbde.project import Model, Project, sql_string

COLUMN_TESTS = {"not_null", "unique", "accepted_values", "relationships",
                "accepted_range", "expression"}
TABLE_TESTS = {"expression", "unique_combination", "row_count_between",
               "recency", "reconciles_to"}
SEVERITIES = {"error", "warn"}


@dataclass
class Test:
    """One data quality test.

    Attributes:
        layer: ``source``, ``silver`` or ``gold``.
        table: ``schema.Table`` (or ``source.table`` for bronze).
        name: Unique, readable test name.
        kind: Test type, e.g. ``unique``.
        column: Column under test, or None for table tests.
        args: Test arguments.
        severity: ``error`` (stops the load) or ``warn`` (logged only).
        implied: True when the kit added the test, not the spec author.
    """

    layer: str
    table: str
    name: str
    kind: str
    column: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"
    implied: bool = False


def _parse(entry: Any) -> tuple:
    """Split a spec test entry into (kind, args, severity).

    Args:
        entry: ``"unique"`` or ``{"accepted_values": {...}}``.

    Returns:
        The test kind, its arguments and its severity.
    """
    if isinstance(entry, str):
        return entry, {}, "error"
    if isinstance(entry, dict) and len(entry) == 1:
        kind, args = next(iter(entry.items()))
        if args is None:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        args = dict(args)
        severity = str(args.pop("severity", "error"))
        return str(kind), args, severity
    return "invalid", {"entry": entry}, "error"


def implied_tests(model: Model) -> List[Test]:
    """Return the tests every table gets without asking.

    Args:
        model: A silver or gold model.

    Returns:
        Key tests for silver; surrogate key, business key, SCD2 and
        foreign key tests for gold.
    """
    out: List[Test] = []
    keys = model.business_keys

    def add(kind: str, column: Optional[str] = None, **args: Any) -> None:
        label = column or "_".join(args.get("columns", [])) or "table"
        out.append(Test(model.layer, model.key, f"{kind}__{label}", kind,
                        column, args, "error", True))

    for key in keys:
        add("not_null", key)
    if model.is_scd2:
        if keys:
            add("one_current_row", columns=keys)
            add("no_overlapping_versions", columns=keys)
        add("expression", expression="StartDate <= EndDate")
    elif len(keys) == 1:
        add("unique", keys[0])
    elif keys:
        add("unique_combination", columns=keys)
    if model.layer == "gold":
        add("not_null", model.surrogate_key)
        add("unique", model.surrogate_key)
        for col in model.columns:
            if col.references:
                dim = col.references.split(".")[-1]
                add("not_null", col.name)
                add("relationships", col.name, to=col.references,
                    field=f"{dim}_SK")
    return out


def declared_tests(model: Model) -> List[Test]:
    """Return the tests written in the spec.

    Args:
        model: A silver or gold model.

    Returns:
        Column tests then table tests, in spec order.
    """
    out: List[Test] = []
    for col in model.columns:
        for entry in col.tests:
            kind, args, severity = _parse(entry)
            out.append(Test(model.layer, model.key, f"{kind}__{col.name}",
                            kind, col.name, args, severity))
    for i, entry in enumerate(model.tests, 1):
        kind, args, severity = _parse(entry)
        out.append(Test(model.layer, model.key, f"{kind}__table_{i}", kind,
                        None, args, severity))
    return out


def source_tests(project: Project) -> List[Test]:
    """Return the freshness tests for every bronze table with a freshness rule.

    Args:
        project: The loaded project.

    Returns:
        A ``warn`` and an ``error`` recency test per bronze table.
    """
    out: List[Test] = []
    for source in project.sources.values():
        rule = source.freshness
        column = rule.get("loaded_at_column")
        if not column:
            continue
        for table in source.tables.values():
            for severity in ("warn", "error"):
                hours = rule.get(f"{severity}_after_hours")
                if hours is None:
                    continue
                out.append(Test("source", f"{source.name}.{table.name}",
                                f"freshness_{severity}__{column}", "recency",
                                None, {"column": column,
                                       "max_age_hours": int(hours)},
                                severity, True))
    return out


def all_tests(project: Project, layer: Optional[str] = None) -> List[Test]:
    """Return every test in the project, optionally for one layer.

    Args:
        project: The loaded project.
        layer: ``source``, ``silver``, ``gold`` or None for all.

    Returns:
        Tests in a stable order: source, silver, gold.
    """
    tests = source_tests(project)
    for model in project.silver() + project.gold():
        tests += implied_tests(model) + declared_tests(model)
    unique: Dict[tuple, Test] = {}
    for test in tests:
        if layer in (None, test.layer):
            # A spec may repeat an implied test (e.g. not_null on a key).
            unique.setdefault((test.table, test.name), test)
    return list(unique.values())


def _q(name: str) -> str:
    """Backtick-quote a Databricks SQL identifier.

    Args:
        name: Column or object name.

    Returns:
        ```name``` with backticks escaped.
    """
    return "`" + name.replace("`", "``") + "`"


def _literal(value: Any) -> str:
    """Return a Databricks SQL literal for a test value.

    Args:
        value: A string, number or boolean from the spec.

    Returns:
        The literal text.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return sql_string(str(value))


def relation_for(project: Project, test: Test,
                 catalogs: Optional[Dict[str, str]] = None) -> str:
    """Return the three-part name of the table a test runs on.

    Args:
        project: The loaded project.
        test: The test.
        catalogs: Layer -> catalog, or None for the catalog tokens.

    Returns:
        The relation name.
    """
    if test.layer == "source":
        source, table = test.table.split(".", 1)
        return project.source_relation(source, table, catalogs)
    return project.relation(project.models[test.table], catalogs)


def _target_relation(project: Project, key: str,
                     catalogs: Optional[Dict[str, str]]) -> str:
    """Return the three-part name of a referenced model.

    Args:
        project: The loaded project.
        key: ``schema.Table``.
        catalogs: Layer -> catalog, or None for the catalog tokens.

    Returns:
        The relation name, or the key itself when unknown.
    """
    model = project.models.get(key)
    return project.relation(model, catalogs) if model else key


def failing_rows_sql(project: Project, test: Test,
                     catalogs: Optional[Dict[str, str]] = None) -> str:
    """Return a query that selects the rows that fail a test.

    Args:
        project: The loaded project.
        test: The test.
        catalogs: Layer -> catalog, or None for the catalog tokens.

    Returns:
        A single SELECT statement (no trailing semicolon).

    Raises:
        ValueError: If the test kind is unknown.
    """
    rel = relation_for(project, test, catalogs)
    col = _q(test.column) if test.column else None
    a = test.args
    kind = test.kind
    if kind == "not_null":
        return f"select {col} from {rel} where {col} is null"
    if kind == "unique":
        return (f"select {col} from {rel} where {col} is not null "
                f"group by {col} having count(*) > 1")
    if kind in ("unique_combination", "one_current_row"):
        cols = ", ".join(_q(c) for c in a.get("columns") or [])
        if kind == "one_current_row":
            return (f"select {cols} from {rel} group by {cols} having "
                    "count_if(`IsCurrent`) <> 1")
        return f"select {cols} from {rel} group by {cols} having count(*) > 1"
    if kind == "no_overlapping_versions":
        keys = a.get("columns") or []
        sk = _q(test.table.split(".")[-1] + "_SK")
        on = " and ".join(f"earlier.{_q(k)} = later.{_q(k)}" for k in keys)
        return (f"select earlier.{sk} from {rel} as earlier "
                f"inner join {rel} as later on {on} "
                f"and earlier.{sk} < later.{sk} "
                "and earlier.`StartDate` <= later.`EndDate` "
                "and later.`StartDate` <= earlier.`EndDate`")
    if kind == "accepted_values":
        values = ", ".join(_literal(v) for v in a.get("values") or [])
        return (f"select {col} from {rel} where {col} is not null "
                f"and {col} not in ({values})")
    if kind == "relationships":
        parent = _target_relation(project, str(a.get("to")), catalogs)
        fld = _q(str(a.get("field")))
        return (f"select child.{col} from {rel} as child "
                f"left join {parent} as parent on child.{col} = parent.{fld} "
                f"where child.{col} is not null and parent.{fld} is null")
    if kind == "accepted_range":
        checks = []
        if a.get("min") is not None:
            checks.append(f"{col} < {_literal(a['min'])}")
        if a.get("max") is not None:
            checks.append(f"{col} > {_literal(a['max'])}")
        return (f"select {col} from {rel} where "
                + (" or ".join(checks) or "1 = 0"))
    if kind == "expression":
        expr = a.get("expression") or a.get("value")
        return f"select * from {rel} where not ({expr})"
    if kind == "row_count_between":
        checks = []
        if a.get("min") is not None:
            checks.append(f"row_count < {int(a['min'])}")
        if a.get("max") is not None:
            checks.append(f"row_count > {int(a['max'])}")
        return (f"select row_count from (select count(*) as row_count "
                f"from {rel}) as counted where "
                + (" or ".join(checks) or "1 = 0"))
    if kind == "recency":
        column = _q(str(a.get("column")))
        hours = int(a.get("max_age_hours") or 24 * int(a.get("max_age_days")
                                                       or 1))
        return (f"select latest from (select max({column}) as latest "
                f"from {rel}) as newest where latest is null or "
                f"timestampdiff(HOUR, latest, current_timestamp()) > {hours}")
    if kind == "reconciles_to":
        return _reconciles_sql(project, rel, a, catalogs)
    raise ValueError(f"unknown test type '{kind}'")


def _reconciles_sql(project: Project, rel: str, a: Dict[str, Any],
                    catalogs: Optional[Dict[str, str]]) -> str:
    """Return the reconciliation query: totals that differ by more than a
    tolerance from a control table.

    Args:
        project: The loaded project.
        rel: The tested table.
        a: ``column``, ``compare_to``, ``compare_column``, ``group_by``,
            ``tolerance``.
        catalogs: Layer -> catalog, or None for the catalog tokens.

    Returns:
        A SELECT that returns one row per group out of tolerance.
    """
    groups = [_q(g) for g in a.get("group_by") or []]
    compare = _target_relation(project, str(a.get("compare_to")), catalogs)
    tolerance = a.get("tolerance", 0)
    select_groups = "".join(f"{g}, " for g in groups)
    group_by = f" group by {', '.join(groups)}" if groups else ""
    on = " and ".join(f"actual.{g} = expected.{g}" for g in groups) or "1 = 1"
    keys = "".join(f"coalesce(actual.{g}, expected.{g}) as {g}, "
                   for g in groups)
    return (
        f"select {keys}coalesce(actual.actual_total, 0) as actual_total, "
        "coalesce(expected.expected_total, 0) as expected_total "
        f"from (select {select_groups}sum({_q(str(a.get('column')))}) "
        f"as actual_total from {rel}{group_by}) as actual "
        f"full outer join (select {select_groups}"
        f"sum({_q(str(a.get('compare_column')))}) as expected_total "
        f"from {compare}{group_by}) as expected on {on} "
        "where abs(coalesce(actual.actual_total, 0) - "
        f"coalesce(expected.expected_total, 0)) > {_literal(tolerance)}")


def count_sql(project: Project, test: Test,
              catalogs: Optional[Dict[str, str]] = None) -> str:
    """Return a query that counts the failing rows of a test.

    Args:
        project: The loaded project.
        test: The test.
        catalogs: Layer -> catalog, or None for the catalog tokens.

    Returns:
        ``select count(*) as failures from (...) as failing``.
    """
    return (f"select count(*) as failures from "
            f"({failing_rows_sql(project, test, catalogs)}) as failing")
