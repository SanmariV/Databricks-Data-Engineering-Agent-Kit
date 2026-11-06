"""Tests for the dbde toolkit and the shared notebook library.

They run offline: no Databricks workspace is needed. The worked example in
``examples/insurance`` is the main fixture.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sqlglot
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "insurance"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from databricks_de_common import ops, silver  # noqa: E402
from dbde import check, connect, dq, manifest, render  # noqa: E402
from dbde.project import load_project, substitute  # noqa: E402

CATALOGS = {"bronze": "dev_bronze", "silver": "dev_silver",
            "gold": "dev_gold"}


@pytest.fixture()
def example():
    """Return the loaded example project."""
    return load_project(EXAMPLE)


@pytest.fixture()
def scratch(tmp_path):
    """Return a writable copy of the example project."""
    target = tmp_path / "project"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns("target"))
    return target


def _code_cells(text: str) -> str:
    """Return the code cells of a Databricks source-format notebook."""
    cells = text.split("# COMMAND ----------")
    return "\n".join(c for c in cells
                     if not c.strip().startswith("# MAGIC")
                     and not c.strip().startswith("# Databricks notebook"))


def _merge(sql: str) -> str:
    """Return the MERGE statement inside a load procedure."""
    start = sql.index("MERGE INTO")
    return sql[start:sql.index(");\nEND;", start) + 1]


def _bind(sql: str) -> str:
    """Replace procedure variables with literals so sqlglot can parse."""
    for name, value in [("RunLoadDate", "DATE'2026-11-05'"),
                        ("RunLoadStepID", "'1'"),
                        ("RunTimestamp", "current_timestamp()"),
                        ("MaxKey", "0")]:
        sql = re.sub(rf"\b{name}\b", value, sql)
    return sql


def test_example_has_no_errors(example):
    findings = check.run_checks(example)
    assert [f for f in findings if f.severity == "error"] == []


def test_example_render_is_committed_and_current(example):
    assert render.write(example, check=True) == []


def test_render_is_deterministic(example):
    assert render.render_all(example) == render.render_all(example)


def test_rendered_sql_names_no_catalog(example):
    for path, content in render.render_all(example).items():
        if path == "databricks.yml":
            continue   # the one place catalogs are set, per target
        for name in CATALOGS.values():
            assert name not in content, path


def test_notebooks_are_valid_python(example):
    for path, content in render.render_all(example).items():
        if not path.startswith("notebooks/"):
            continue
        assert content.startswith("# Databricks notebook source\n"), path
        code = _code_cells(content)
        compile(code, path, "exec")
        assert content.count("# MAGIC %md") >= 3, path
        for line in code.splitlines():
            assert len(line) <= 79, f"{path}: {line}"
        if path.startswith("notebooks/silver/"):
            assert "def main(" in code
            assert "dbutils.notebook.exit" in code


def test_silver_notebook_query_resolves_at_run_time(example):
    content = render.render_all(example)[
        "notebooks/silver/nb_silver_sales_new_business_policy.py"]
    assert "${silver_catalog}.policy_admin.Policy" in content
    assert "silver.substitute(QUERY, catalogs)" in content


def test_gold_statements_parse(example):
    rendered = render.render_all(example)
    for path in render.gold_deploy_order(example):
        for statement in connect.statements(rendered[path]):
            sql = substitute(statement, CATALOGS)
            body = re.sub(r"--[^\n]*", "", sql).strip()
            if body.startswith("BEGIN") or "PROCEDURE" in sql:
                continue   # SQL scripting: covered by the tests below
            if "WITH METRICS" in sql:
                continue   # metric view YAML: tested in test_genie.py
            sqlglot.parse_one(sql, read="databricks")


def test_load_procedures_parse(example):
    for model in example.gold():
        sql = substitute(render.gold_procedure_sql(example, model), CATALOGS)
        assert sql.count("CREATE OR REPLACE PROCEDURE") == 1
        assert "LANGUAGE SQL" in sql and "SQL SECURITY INVOKER" in sql
        assert "RunLoadDate DATE" in sql and "RunLoadStepID STRING" in sql
        sqlglot.parse_one(_bind(_merge(sql)), read="databricks")


def test_scd2_procedure_closes_and_inserts(example):
    sql = render.gold_procedure_sql(example,
                                    example.models["distribution.DimAgent"])
    assert "EndDate = date_sub(RunLoadDate, 1)" in sql
    assert "CAST(NULL AS VARCHAR(20)) AS MergeKey1" in sql
    assert "AND target.IsCurrent" in sql
    assert " OR OR " not in sql


def test_fact_procedure_filters_incrementally(example):
    sql = render.gold_procedure_sql(example,
                                    example.models["sales.FactNewBusiness"])
    assert "source_rows.IssueDate >= date_sub(RunLoadDate, 7)" in sql
    assert "WITH source_rows AS (" in sql


def test_gold_table_has_keys_and_unknown_member(example):
    dim = render.gold_table_sql(example,
                                example.models["distribution.DimAgent"])
    assert "CONSTRAINT DimAgent_pk PRIMARY KEY (DimAgent_SK)" in dim
    assert "WHERE DimAgent_SK = -1" in dim
    fact = render.gold_table_sql(example,
                                 example.models["sales.FactNewBusiness"])
    assert ("FOREIGN KEY (DimAgent_SK) REFERENCES "
            "${gold_catalog}.distribution.DimAgent (DimAgent_SK)") in fact
    create = fact[fact.index("CREATE TABLE"):fact.index("USING DELTA")]
    columns = [c for c in re.findall(r"^    (\w+)\s+[A-Z]", create,
                                     flags=re.M) if c != "CONSTRAINT"]
    assert columns[0] == "FactNewBusiness_SK"
    assert columns[1] == "Policy_BK"
    assert columns[-4:] == ["InsertLoadStepID", "InsertTimestamp",
                            "LastUpdateLoadStepID", "LastUpdateTimestamp"]


def test_ops_tests_procedure(example):
    sql = render.ops_tests_sql(example)
    for layer in ("source", "silver", "gold"):
        assert f"IF TestLayer = '{layer}' THEN" in sql
    assert "SIGNAL SQLSTATE '45000'" in sql


def test_every_test_query_parses(example):
    tests = dq.all_tests(example)
    assert {t.layer for t in tests} == {"source", "silver", "gold"}
    for test in tests:
        sqlglot.parse_one(dq.count_sql(example, test, CATALOGS),
                          read="databricks")


def test_implied_tests(example):
    names = {(t.table, t.name) for t in dq.all_tests(example)}
    assert ("distribution.DimAgent", "one_current_row__Agent_BK") in names
    assert ("distribution.DimAgent",
            "no_overlapping_versions__Agent_BK") in names
    assert ("sales.FactNewBusiness", "relationships__DimAgent_SK") in names
    assert ("policy_admin.Policy", "unique__Policy_BK") in names


def test_load_order_puts_dimensions_first(example):
    order = [m.key for m in example.load_order(example.gold())]
    assert order.index("distribution.DimAgent") < order.index(
        "sales.FactNewBusiness")


def test_refs_resolve_to_tokens_or_catalogs(example):
    sql = "select * from {{ ref('policy_admin.Policy') }}"
    assert example.resolve_sql(sql) == \
        "select * from ${silver_catalog}.policy_admin.Policy"
    assert example.resolve_sql(sql, CATALOGS) == \
        "select * from dev_silver.policy_admin.Policy"


def test_catalogs_default_per_target(example):
    assert example.config.catalogs("prod") == {
        "bronze": "prod_bronze", "silver": "prod_silver",
        "gold": "prod_gold"}


def test_daily_job_orders_tasks(example):
    job = yaml.safe_load(render.daily_job_yml(example))
    tasks = {t["task_key"]: t for t in
             job["resources"]["jobs"]["dbde_daily_load"]["tasks"]}

    def after(key):
        return {d["task_key"] for d in tasks[key].get("depends_on", [])}

    assert after("silver_sales_new_business_policy") == {
        "test_source", "silver_policy_admin_policy"}
    assert "test_silver" in after("gold_distribution_dim_agent")
    assert "gold_distribution_dim_agent" in after(
        "gold_sales_fact_new_business")
    assert tasks["gold_sales_fact_new_business"]["notebook_task"][
        "base_parameters"] == {"procedure": "sales.LoadFactNewBusiness"}
    parameters = {p["name"]: p["default"] for p in
                  job["resources"]["jobs"]["dbde_daily_load"]["parameters"]}
    assert parameters["load_step_id"] == "{{job.run_id}}"
    assert parameters["gold_catalog"] == "${var.gold_catalog}"


def test_bundle_targets(example):
    bundle = yaml.safe_load(render.bundle_yml(example))
    assert bundle["targets"]["dev"]["mode"] == "development"
    assert bundle["targets"]["prod"]["mode"] == "production"
    assert "run_as" in bundle["targets"]["prod"]
    assert bundle["targets"]["test"]["variables"]["gold_catalog"] == \
        "test_gold"
    text = render.bundle_yml(example)
    assert "host:" not in text and "token" not in text.lower()


def test_manifest_shape(example):
    built = manifest.build(example, "prod")
    names = {n["name"] for n in built["nodes"].values()
             if n["resource_type"] == "model"}
    assert {"stg_policy_admin__policy", "int_sales__new_business_policy",
            "dim_agent", "fct_new_business", "mv_new_business"} <= names
    relations = {n["relation_name"] for n in built["nodes"].values()
                 if n["resource_type"] == "model"}
    assert "prod_gold.sales.FactNewBusiness" in relations
    assert "prod_gold.sales.MetricNewBusiness" in relations
    rel_tests = [n for n in built["nodes"].values()
                 if n["resource_type"] == "test"
                 and n["test_metadata"]["name"] == "relationships"]
    assert len(rel_tests[0]["depends_on"]["nodes"]) == 2
    assert any(e["source_column"] == "policy_no" for e in built["lineage"])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_checks_catch_violations(scratch):
    gold = scratch / "models" / "gold" / "sales"
    _write(gold / "FactBad.yml", """
description: Bad fact without a grain.
columns:
  - name: Policy_BK
    data_type: nvarchar(20)
    business_key: true
    description: Key.
  - name: PremAmt
    data_type: double
    description: Money as double.
  - name: Active
    data_type: boolean
    description: Flag without Is.
  - name: AgentKey_SK
    data_type: bigint
    description: Missing references.
  - name: RunLoadDate
    data_type: date
    description: Reserved by the procedure.
""")
    _write(gold / "FactBad.sql",
           "select * from dev_silver.policy_admin.Policy "
           "where IssueDate < current_date() and Policy_BK = :key limit 10")
    rules = {f.rule for f in check.run_checks(load_project(scratch))
             if f.severity == "error"}
    assert {"S2", "D2", "D3", "N9", "G2", "L5", "I1", "S10", "G6",
            "G7"} <= rules


def test_checks_catch_layer_breaks(scratch):
    silver_dir = scratch / "models" / "silver" / "sales"
    _write(silver_dir / "ReadsGold.yml", """
description: Reads gold. Grain = Policy_BK.
columns:
  - {name: Policy_BK, data_type: string, business_key: true, description: Key.}
""")
    _write(silver_dir / "ReadsGold.sql",
           "select Policy_BK from {{ ref('sales.FactNewBusiness') }}")
    rules = {f.rule for f in check.run_checks(load_project(scratch))}
    assert "L3" in rules


def test_double_colon_cast_is_not_a_parameter(scratch):
    sql = scratch / "models" / "gold" / "sales" / "FactNewBusiness.sql"
    sql.write_text(sql.read_text().replace(
        "new_business.ProductCode,", "new_business.ProductCode::string "
        "as ProductCode,"))
    rules = {f.rule for f in check.run_checks(load_project(scratch))}
    assert "G6" not in rules


def test_render_check_detects_hand_edits(scratch):
    target = scratch / "gold" / "sales" / "procedures" / \
        "LoadFactNewBusiness.sql"
    target.write_text(target.read_text() + "-- edited\n")
    messages = render.write(load_project(scratch), check=True)
    assert any("LoadFactNewBusiness" in m for m in messages)


def test_render_removes_stale_files(scratch):
    (scratch / "models" / "gold" / "sales" / "FactNewBusiness.yml").unlink()
    (scratch / "models" / "gold" / "sales" / "FactNewBusiness.sql").unlink()
    (scratch / "models" / "genie" / "SalesAnalyst.yml").unlink()
    messages = render.write(load_project(scratch))
    assert any(m.startswith("removed: gold/sales/") for m in messages)
    assert any(m == "removed: genie/SalesAnalyst.geniespace.json"
               for m in messages)


def test_statements_split_on_go_lines():
    script = ("-- header\nCREATE SCHEMA a;\n-- GO\n\nBEGIN\n  SELECT 1;\n"
              "  SELECT 2;\nEND;\n-- GO\n-- trailing comment\n")
    assert connect.statements(script) == [
        "-- header\nCREATE SCHEMA a", "BEGIN\n  SELECT 1;\n  SELECT 2;\nEND"]


def test_protected_targets_refuse_writes(example, monkeypatch):
    monkeypatch.delenv("DBDE_ALLOW_PROTECTED", raising=False)
    with pytest.raises(connect.TargetError):
        connect.target_settings(example.config, "prod", writes=True)
    connect.target_settings(example.config, "prod", writes=False)
    monkeypatch.setenv("DBDE_ALLOW_PROTECTED", "1")
    connect.target_settings(example.config, "prod", writes=True)


def test_ci_ignores_the_profile(example, monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example")
    assert connect.target_settings(example.config, "dev", False)[
        "profile"] == ""
    monkeypatch.delenv("DATABRICKS_HOST")
    assert connect.target_settings(example.config, "dev", False)[
        "profile"] == "dbde-dev"


def test_read_only_guard():
    connect.assert_read_only("select LastUpdateTimestamp from x")
    connect.assert_read_only("with a as (select 1 as n) select n from a")
    connect.assert_read_only("select 'it''s; -- /*' as a, `x;y` from t -- c")
    connect.assert_read_only("select 'it\\'s' as a from t")
    connect.assert_read_only("select replace(a, 'x', 'y') from t")
    # A backslash escapes the quote: this is one string literal.
    connect.assert_read_only(r"select 'a\'; drop table t; --' as a")
    for bad in ["delete from x", "select 1; drop table x",
                "insert into y select * from x", "call a.b.c()",
                "select '/*' ; delete from t; select '*/'",
                "select 1 /* x */ ; drop table t",
                "select '--' as a; drop table t",
                "select `a`; drop table t", "select 'open",
                "select 1 -- x\rdelete from t", "select 1 commit",
                "optimize t", "with a as (select 1) insert into t select 1"]:
        with pytest.raises(connect.TargetError):
            connect.assert_read_only(bad)


def test_silver_helpers():
    assert silver.resolve_load_date("2026-10-31") == "2026-10-31"
    assert silver.resolve_load_date("", dt.date(2026, 11, 4)) == "2026-11-03"
    with pytest.raises(ValueError):
        silver.resolve_load_date("31/10/2026")
    exprs = silver.select_expressions(
        [("Customer_BK", "string", "upper(id)", "Key.")],
        ["_ingested_at desc"])
    assert exprs == ["cast((upper(id)) as string) as `Customer_BK`",
                     "_ingested_at as `__order_0_desc`"]
    assert silver.change_condition(["A", "B"]) == \
        "not (t.`A` <=> s.`A`) or not (t.`B` <=> s.`B`)"
    assert silver.change_condition([]) is None
    ddl = silver.table_ddl("cat.s.T", [("Name", "string", "n", "A name."),
                                       ("T_BK", "string", "k", "Key.")],
                           ["T_BK"], "It's a table")
    assert ddl.index("`T_BK` string NOT NULL") < ddl.index("`Name` string")
    assert "`LastUpdateTimestamp` timestamp NOT NULL" in ddl
    assert "CONSTRAINT `T_pk` PRIMARY KEY (`T_BK`)" in ddl
    assert "It\\'s" in ddl
    sqlglot.parse_one(ddl, read="databricks")
    assert silver.substitute("select * from ${silver_catalog}.a.B",
                             CATALOGS) == "select * from dev_silver.a.B"
    with pytest.raises(ValueError):
        silver.substitute("${gold_catalog}.x", {"gold": ""})


def test_ops_names_are_checked():
    assert ops._qualified("dev_gold", "sales.LoadFactNewBusiness") == \
        "`dev_gold`.`sales`.`LoadFactNewBusiness`"
    for bad in [("dev_gold", "sales.Load; drop"), ("", "a.b"),
                ("dev_gold", "LoadFactNewBusiness"), ("x`y", "a.b")]:
        with pytest.raises(ValueError):
            ops._qualified(*bad)
    with pytest.raises(ValueError):
        ops.maintain_tables(None, [], retain_hours=24)
    with pytest.raises(ValueError):
        ops.run_data_quality_tests(None, "dev_gold", "bronze", "1")


def test_empty_project_renders(tmp_path):
    shutil.copy(ROOT / "dbde.yml", tmp_path / "dbde.yml")
    project = load_project(tmp_path)
    files = render.render_all(project)
    assert "databricks.yml" in files
    assert "gold/ops/procedures/RunGoldLoads.sql" not in files
    assert [f.rule for f in check.run_checks(project)] == ["P2"] * 3
    with pytest.raises(connect.TargetError):
        connect.target_settings(project.config, "dev", writes=False)


def test_kit_root_render_is_current():
    assert render.write(load_project(ROOT), check=True) == []


def test_cli_check_runs_in_example():
    result = subprocess.run([sys.executable, "-m", "dbde", "check"],
                            cwd=EXAMPLE, capture_output=True, text=True,
                            env={"PYTHONPATH": str(ROOT),
                                 "SYSTEMROOT": "C:\\Windows",
                                 "PATH": ""})
    assert result.returncode == 0, result.stdout + result.stderr
