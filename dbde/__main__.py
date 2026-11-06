"""Command line: ``python -m dbde <command>``.

Offline (no Databricks connection):
    parse          check the specs load and write target/manifest.json
    check          layer, naming, type and completeness rules + render drift
    render         write the notebooks, gold SQL, metric views, Genie spaces
                   and the Asset Bundle (--check: CI)
    ls             list models and tests

Against a workspace (``--target dev`` by default):
    debug            test sign-in, the SQL warehouse and the catalogs
    show             run one read-only SELECT (refs allowed)
    generate-source  write models/sources/<source>.yml from bronze metadata
    deploy           run the gold SQL, then deploy the Asset Bundle
                     (notebooks, jobs, Genie spaces)
    run              run the daily job (or one layer of it) and wait
    test             run the data quality tests and print the results
    test-genie       run every Genie example question (read-only)
    build            deploy, run silver, load gold and test, layer by layer
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

import yaml

from dbde import check as checks
from dbde import connect, dq, genie, manifest, render
from dbde.project import Project, load_project, rel, substitute

ROOT = Path.cwd()


def _project() -> Project:
    """Load the project from the current folder.

    Returns:
        The project.
    """
    return load_project(ROOT)


def _table(rows: List[dict], limit: int = 50) -> str:
    """Format rows as a plain-text table.

    Args:
        rows: Rows as dicts.
        limit: Maximum rows to show.

    Returns:
        The table text.
    """
    if not rows:
        return "(no rows)"
    names = list(rows[0])
    shown = [[str(r.get(n, "")) for n in names] for r in rows[:limit]]
    widths = [max(len(n), *(len(r[i]) for r in shown))
              for i, n in enumerate(names)]
    line = " | ".join(n.ljust(w) for n, w in zip(names, widths))
    out = [line, "-+-".join("-" * w for w in widths)]
    out += [" | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in shown]
    return "\n".join(out)


def cmd_parse(args) -> int:
    """Load every spec and write the manifest.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    for path, message in project.errors:
        print(f"error: {path}: {message}")
    path = manifest.write(project, args.target)
    print(f"{len(project.sources)} sources, {len(project.silver())} silver, "
          f"{len(project.gold())} gold, {len(project.spaces)} Genie spaces, "
          f"{len(dq.all_tests(project))} tests. Wrote {path} "
          f"({args.target} catalogs)")
    return 1 if project.errors else 0


def cmd_check(args) -> int:
    """Run the project checks and the render drift check.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code (1 when any error finding or drift).
    """
    project = _project()
    findings = checks.run_checks(project)
    drift = render.write(project, check=True) if not project.errors else []
    for f in findings:
        print(f"{f.severity:5} {f.rule:4} {f.file}: {f.message}")
    for message in drift:
        print(f"error R1   {message} (run python -m dbde render)")
    errors = sum(f.severity == "error" for f in findings) + len(drift)
    warns = sum(f.severity == "warn" for f in findings)
    print(f"check: {errors} errors, {warns} warnings")
    return 1 if errors else 0


def cmd_render(args) -> int:
    """Write (or check) the rendered files.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    if project.errors:
        for path, message in project.errors:
            print(f"error: {path}: {message}")
        return 1
    messages = render.write(project, check=args.check)
    for message in messages:
        print(message)
    if args.check:
        print("render: up to date" if not messages
              else f"render: {len(messages)} files out of date")
        return 1 if messages else 0
    print(f"render: {len(messages)} files changed")
    return 0


def cmd_ls(args) -> int:
    """List models (and tests).

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    for source in project.sources.values():
        for table in source.tables:
            print(f"source  {source.name}.{table}")
    for model in project.silver() + project.gold():
        print(f"{model.layer:6}  {model.key:40} {model.kind:10} "
              f"keys={','.join(model.business_keys)}")
    for key in project.metric_views:
        print(f"{'gold':6}  {key:40} {'metric':10}")
    for space in project.spaces.values():
        print(f"genie   {space.name:40} tables={','.join(space.tables)}")
    if args.tests:
        for test in dq.all_tests(project):
            print(f"test    {test.table:40} {test.name:45} {test.severity}"
                  f"{' (implied)' if test.implied else ''}")
    return 0


def cmd_debug(args) -> int:
    """Test sign-in, the SQL warehouse and the target's catalogs.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    settings = connect.target_settings(project.config, args.target, False)
    with connect.sql_connection(project.config, args.target) as conn:
        print(_table(connect.query(
            conn, "select current_user() as signed_in_as, "
                  "current_version().dbsql_version as dbsql_version")))
        found = {r["catalog"] for r in connect.query(conn, "show catalogs")}
    wanted = settings["catalogs"]
    print("Catalogs:", ", ".join(f"{layer}={name}"
                                 for layer, name in wanted.items()))
    missing = [name for name in wanted.values() if name not in found]
    if missing:
        print(f"Missing catalogs (ask an admin to create them, or fix "
              f"dbde.yml): {missing}")
        return 1
    print("debug: connection OK")
    return 0


def cmd_show(args) -> int:
    """Run one read-only SELECT and print the rows.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    catalogs = project.config.catalogs(args.target)
    sql_text = project.resolve_sql(args.sql, catalogs)
    with connect.sql_connection(project.config, args.target) as conn:
        print(_table(connect.read_only_query(conn, sql_text, args.limit),
                     args.limit))
    return 0


def cmd_generate_source(args) -> int:
    """Write ``models/sources/<source>.yml`` from the bronze metadata.

    Existing descriptions, freshness and meta are kept; new tables and
    columns are added; dropped ones are reported.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    schema = (args.schema or project.config.layer_schemas.get("bronze")
              or args.source).replace("'", "")
    catalog = project.config.catalogs(args.target)["bronze"]
    sql_text = (f"select table_name, column_name, full_data_type, comment "
                f"from {catalog}.information_schema.columns "
                f"where table_schema = '{schema}' "
                "order by table_name, ordinal_position")
    with connect.sql_connection(project.config, args.target) as conn:
        rows = connect.query(conn, sql_text)
    if not rows:
        print(f"No tables found in {catalog}.{schema}. Check the schema name "
              "and that you have USE SCHEMA and SELECT on it.")
        return 1
    path = ROOT / "models" / "sources" / f"{args.source}.yml"
    old = yaml.safe_load(path.read_text(encoding="utf-8")) \
        if path.exists() else {}
    old_tables = {t["name"]: t for t in (old or {}).get("tables") or []}
    defaults = (project.config.raw.get("defaults") or {}).get("freshness")
    tables: Dict[str, list] = {}
    for row in rows:
        tables.setdefault(row["table_name"], []).append(row)
    out_tables = []
    for name, cols in tables.items():
        before = old_tables.get(name, {})
        old_cols = {c["name"]: c for c in before.get("columns") or []
                    if isinstance(c, dict)}
        out_cols = []
        for col in cols:
            entry = {**old_cols.get(col["column_name"], {}),
                     "name": col["column_name"],
                     "data_type": str(col["full_data_type"]).lower()}
            if col.get("comment") and "description" not in entry:
                entry["description"] = col["comment"]
            out_cols.append(entry)
        out_tables.append({
            "name": name,
            "description": before.get("description")
            or f"TODO(what one row of {name} is)",
            **({"meta": before["meta"]} if before.get("meta") else {}),
            "columns": out_cols,
        })
        gone = set(old_cols) - {c["column_name"] for c in cols}
        if gone:
            print(f"warning: {name}: columns no longer in bronze: "
                  f"{sorted(gone)}")
    for name in set(old_tables) - set(tables):
        print(f"warning: table {name} is no longer in bronze; removed")
    doc = {
        "source": args.source,
        "description": (old or {}).get("description")
        or f"TODO(what the {args.source} system is and how it lands)",
        **({"schema": schema} if schema != args.source else {}),
        "freshness": (old or {}).get("freshness") or defaults or {
            "loaded_at_column": "_ingested_at", "warn_after_hours": 26,
            "error_after_hours": 48},
        "tables": out_tables,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=79,
                                   allow_unicode=True), encoding="utf-8")
    print(f"Wrote {rel(path, ROOT)}: {len(out_tables)} tables, "
          f"{len(rows)} columns")
    return 0


def _require_current(project: Project) -> None:
    """Refuse to deploy rendered files that are out of date.

    Raises:
        connect.TargetError: If ``python -m dbde render`` would change files.
    """
    if project.errors or render.write(project, check=True):
        raise connect.TargetError("rendered files are out of date or specs "
                                  "do not load; run python -m dbde render")


def _deploy_gold(project: Project, target: str) -> None:
    """Run the rendered gold SQL against a target, in dependency order.

    Args:
        project: The loaded project.
        target: Target name.
    """
    catalogs = project.config.catalogs(target)
    with connect.sql_connection(project.config, target, writes=True) as conn:
        for path in render.gold_deploy_order(project):
            script = substitute((ROOT / path).read_text(encoding="utf-8"),
                                catalogs)
            count = connect.execute_script(conn, script)
            print(f"deploy: {path} ({count} statements)")


def _write_genie(project: Project, target: str) -> None:
    """Write each Genie space for a target to ``target/genie/``.

    Args:
        project: The loaded project.
        target: Target name.
    """
    catalogs = project.config.catalogs(target)
    out = ROOT / "target" / "genie"
    out.mkdir(parents=True, exist_ok=True)
    for space in project.spaces.values():
        text = (ROOT / "genie" / f"{space.name}.geniespace.json").read_text(
            encoding="utf-8")
        json.loads(substitute(text, catalogs))   # fail early on bad JSON
        (out / f"{space.name}.geniespace.json").write_text(
            substitute(text, catalogs), encoding="utf-8")


def _deploy_bundle(project: Project, target: str) -> None:
    """Deploy the Asset Bundle: notebooks, library, jobs, Genie spaces.

    Args:
        project: The loaded project.
        target: Target name.
    """
    settings = connect.target_settings(project.config, target, writes=True)
    _write_genie(project, target)
    connect.bundle(["deploy"], settings, target, str(ROOT))


def cmd_deploy(args) -> int:
    """Deploy to a target: gold SQL, then the bundle.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    _require_current(project)
    if not args.bundle_only:
        _deploy_gold(project, args.target)
    if not args.sql_only:
        _deploy_bundle(project, args.target)
    print(f"deploy: {args.target} OK")
    return 0


def _task_keys(project: Project, layer: str, skip_source: bool,
               tests: bool = True) -> List[str]:
    """Return the job task keys of the selected layers.

    Args:
        project: The loaded project.
        layer: ``all``, ``silver`` or ``gold``.
        skip_source: Leave out the source (freshness) tests.
        tests: Include the data quality test tasks.

    Returns:
        Task keys of the daily job.
    """
    keys: List[str] = []
    if layer in ("all", "silver"):
        if tests and not skip_source:
            keys.append("test_source")
        silver = project.silver()
        keys += [render.silver_task_key(m) for m in silver]
        if tests and silver:
            keys.append("test_silver")
    if layer in ("all", "gold") and project.gold():
        keys += [render.gold_task_key(m) for m in project.gold()]
        if tests:
            keys.append("test_gold")
    return keys


def _run(project: Project, target: str, layer: str, load_date: str,
         full_refresh: bool, skip_source: bool, tests: bool = True) -> None:
    """Run the daily job (or some of its tasks) and wait for it.

    Args:
        project: The loaded project.
        target: Target name.
        layer: ``all``, ``silver`` or ``gold``.
        load_date: Business date (empty for yesterday).
        full_refresh: Make source notebooks read all of bronze.
        skip_source: Leave out the source (freshness) tests.
        tests: Include the data quality test tasks.
    """
    settings = connect.target_settings(project.config, target, writes=True)
    keys = _task_keys(project, layer, skip_source, tests)
    if not keys:
        print("run: nothing to run")
        return
    params = f"load_date={load_date},full_refresh={str(full_refresh).lower()}"
    command = ["run", render.JOB, "--params", params]
    if layer != "all" or skip_source or not tests:
        command += ["--only", ",".join(keys)]
    connect.bundle(command, settings, target, str(ROOT))


def cmd_run(args) -> int:
    """Run the daily job on a target.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    _run(_project(), args.target, args.layer, args.load_date or "",
         args.full_refresh, args.skip_source_tests)
    return 0


def _test(project: Project, conn, layer: str, catalogs: Dict[str, str],
          select: str = "") -> int:
    """Run the tests of one layer and print the results.

    Args:
        project: The loaded project.
        conn: Open connection.
        layer: ``source``, ``silver`` or ``gold``.
        catalogs: Layer -> catalog of the target.
        select: Only tests on tables whose key contains this text.

    Returns:
        The number of failed error-severity tests (SQL errors count).
    """
    failed = 0
    rows = []
    for test in dq.all_tests(project, layer):
        if select and select.lower() not in test.table.lower():
            continue
        try:
            count = connect.query(conn, dq.count_sql(project, test,
                                                     catalogs))[0]["failures"]
            status = "PASS" if not count else (
                "FAIL" if test.severity == "error" else "WARN")
        except Exception as exc:  # report every broken test, then fail
            count, status = str(exc).splitlines()[0][:80], "ERROR"
        if status in ("FAIL", "ERROR"):
            failed += 1
        rows.append({"status": status, "table": test.table,
                     "test": test.name, "failures": count})
    if rows:
        print(_table(rows, limit=10000))
    passed = sum(r["status"] == "PASS" for r in rows)
    print(f"test {layer}: {len(rows)} tests, {passed} pass, "
          f"{sum(r['status'] == 'WARN' for r in rows)} warn, {failed} fail")
    return failed


def cmd_test(args) -> int:
    """Run data quality tests on a target.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    catalogs = project.config.catalogs(args.target)
    layers = [args.layer] if args.layer != "all" else \
        ["source", "silver", "gold"]
    failed = 0
    with connect.sql_connection(project.config, args.target) as conn:
        for layer in layers:
            failed += _test(project, conn, layer, catalogs,
                            args.select or "")
    return 1 if failed else 0


def cmd_test_genie(args) -> int:
    """Run the example questions of every Genie space, read-only.

    An example passes when its query runs on the SQL warehouse and returns
    rows. This tests the SQL Genie is shown, not Genie's own answers: ask
    Genie the same questions in Databricks to compare.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code (1 when any example fails).
    """
    project = _project()
    chosen = [s for s in project.spaces.values()
              if not args.select or args.select.lower() in s.name.lower()]
    if not chosen:
        print("test-genie: no Genie spaces (models/genie/<Name>.yml)")
        return 1
    catalogs = project.config.catalogs(args.target)
    failed = 0
    with connect.sql_connection(project.config, args.target) as conn:
        for space in chosen:
            for question, passed, detail in genie.run_examples(
                    project, space, conn, catalogs):
                failed += not passed
                print(f"{'pass' if passed else 'FAIL'}  {space.name}: "
                      f"{question} ({detail})")
    print(f"test-genie: {failed} failed")
    return 1 if failed else 0


def cmd_build(args) -> int:
    """Deploy, run and test the selected layers, stopping at a failure.

    Order: the bundle (notebooks, library, jobs); the silver notebooks;
    the source and silver tests (run from here, so a first build works
    before the ops procedures exist); the gold SQL (tables, load and test
    procedures, metric views); the gold loads and gold tests; and the
    bundle again when there are Genie spaces (they read the gold tables
    and metric views, which now exist).

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    project = _project()
    _require_current(project)
    target = args.target
    load_date = args.load_date or ""
    silver = args.layer in ("all", "silver") and project.silver()
    gold = args.layer in ("all", "gold") and project.gold()
    catalogs = project.config.catalogs(target)
    if not args.skip_deploy:
        _deploy_bundle(project, target)
    if silver:
        _run(project, target, "silver", load_date, args.full_refresh,
             True, tests=False)
        layers = ([] if args.skip_source_tests else ["source"]) + ["silver"]
        with connect.sql_connection(project.config, target) as conn:
            failed = sum(_test(project, conn, layer, catalogs)
                         for layer in layers)
        if failed:
            print("build: silver tests failed; gold not loaded")
            return 1
    if gold:
        if not args.skip_deploy:
            _deploy_gold(project, target)
        _run(project, target, "gold", load_date, False, True, tests=True)
        if project.spaces and not args.skip_deploy:
            _deploy_bundle(project, target)
        with connect.sql_connection(project.config, target) as conn:
            if _test(project, conn, "gold", catalogs):
                print("build: gold tests failed")
                return 1
    print("build: OK")
    return 0


def main(argv=None) -> int:
    """Parse arguments and run a command.

    Args:
        argv: Arguments (default: sys.argv).

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(prog="python -m dbde",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, target=False):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        if target:
            p.add_argument("--target", default="dev")
        return p

    add("parse", cmd_parse, "load specs, write target/manifest.json",
        target=True)
    add("check", cmd_check, "project rules and render drift")
    p = add("render", cmd_render, "write notebooks, SQL and the bundle")
    p.add_argument("--check", action="store_true",
                   help="change nothing; fail when files are out of date")
    p = add("ls", cmd_ls, "list models")
    p.add_argument("--tests", action="store_true")
    add("debug", cmd_debug, "test the connection", target=True)
    p = add("show", cmd_show, "run one read-only SELECT", target=True)
    p.add_argument("sql")
    p.add_argument("--limit", type=int, default=50)
    p = add("generate-source", cmd_generate_source,
            "write a source spec from bronze metadata", target=True)
    p.add_argument("source")
    p.add_argument("--schema", help="bronze schema (default: the source name)")
    p = add("deploy", cmd_deploy, "run the gold SQL and deploy the bundle",
            target=True)
    p.add_argument("--sql-only", action="store_true",
                   help="only the gold SQL")
    p.add_argument("--bundle-only", action="store_true",
                   help="only the bundle (notebooks, jobs, Genie spaces)")
    p = add("run", cmd_run, "run the daily job", target=True)
    p.add_argument("--layer", default="all", choices=["all", "silver", "gold"])
    p.add_argument("--load-date")
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--skip-source-tests", action="store_true")
    p = add("test", cmd_test, "run data quality tests", target=True)
    p.add_argument("--layer", default="all",
                   choices=["all", "source", "silver", "gold"])
    p.add_argument("--select", help="only tables whose name contains this")
    p = add("test-genie", cmd_test_genie,
            "run Genie example questions", target=True)
    p.add_argument("--select", help="only spaces whose name contains this")
    p = add("build", cmd_build, "deploy, run and test", target=True)
    p.add_argument("--layer", default="all", choices=["all", "silver", "gold"])
    p.add_argument("--load-date")
    p.add_argument("--full-refresh", action="store_true")
    p.add_argument("--skip-deploy", action="store_true")
    p.add_argument("--skip-source-tests", action="store_true")

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except connect.TargetError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
