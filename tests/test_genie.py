"""Tests for metric views and Genie spaces (offline)."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import sqlglot
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "insurance"
sys.path.insert(0, str(ROOT))

from dbde import check, genie, render  # noqa: E402
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


def _view_yaml(sql: str) -> dict:
    """Return the YAML body of a rendered metric view."""
    return yaml.safe_load(sql.split("$$")[1])


def _rules(project) -> set:
    """Return the rules of every error finding."""
    return {f.rule for f in check.run_checks(project)
            if f.severity == "error"}


def test_metric_view_name_and_shape(example):
    fact = example.models["sales.FactNewBusiness"]
    assert fact.metric_view == "MetricNewBusiness"
    sql = genie.metric_view_sql(example, fact)
    assert "CREATE OR REPLACE VIEW ${gold_catalog}.sales.MetricNewBusiness" \
        in sql
    assert "WITH METRICS" in sql and "LANGUAGE YAML" in sql
    body = _view_yaml(sql)
    assert body["version"] == "1.1"
    assert body["source"] == "${gold_catalog}.sales.FactNewBusiness"
    assert body["joins"] == [{
        "name": "DimAgent",
        "source": "${gold_catalog}.distribution.DimAgent",
        "on": "source.DimAgent_SK = DimAgent.DimAgent_SK"}]
    assert [m["name"] for m in body["measures"]] == [
        "NewBusinessCount", "AnnualPremiumTotal"]


def test_metric_view_leaves_out_pii_keys_and_numbers(example):
    body = _view_yaml(genie.metric_view_sql(
        example, example.models["sales.FactNewBusiness"]))
    names = {d["name"] for d in body["dimensions"]}
    assert "AgentName" not in names                # meta.pii
    assert "DimAgent_SK" not in names              # surrogate key
    assert "AnnualPremiumAmount" not in names      # numeric: a measure
    assert {"ProductCode", "IssueDate", "BranchCode", "IsActive"} <= names
    assert "AgentName" not in json.dumps(body)


def test_metric_view_keeps_synonyms_and_comments(example):
    body = _view_yaml(genie.metric_view_sql(
        example, example.models["sales.FactNewBusiness"]))
    product = next(d for d in body["dimensions"]
                   if d["name"] == "ProductCode")
    assert product["synonyms"] == ["Product", "Plan"]
    assert product["comment"] == "Product code."
    assert body["measures"][0]["synonyms"] == ["Policies sold",
                                               "Policy count"]


def test_space_json_shape(example):
    space = example.spaces["SalesAnalyst"]
    data = json.loads(genie.space_json(example, space))
    assert data["version"] == 2
    tables = [t["identifier"] for t in data["data_sources"]["tables"]]
    views = [t["identifier"] for t in data["data_sources"]["metric_views"]]
    assert tables == ["${gold_catalog}.sales.FactNewBusiness"]
    assert views == ["${gold_catalog}.sales.MetricNewBusiness"]
    examples = data["instructions"]["example_question_sqls"]
    assert len(examples) == 4
    ids = [e["id"] for e in examples]
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)
    assert "{{" not in json.dumps(data)


def test_space_json_resolves_per_target(example):
    text = genie.space_json(example, example.spaces["SalesAnalyst"])
    resolved = json.loads(substitute(text, CATALOGS))
    assert resolved["data_sources"]["tables"][0]["identifier"] == \
        "dev_gold.sales.FactNewBusiness"


def test_space_resource(example):
    resource = genie.space_resource(example, example.spaces["SalesAnalyst"])
    space = resource["resources"]["genie_spaces"]["genie_sales_analyst"]
    assert space["warehouse_id"] == "${var.warehouse_id}"
    assert space["file_path"] == \
        "../target/genie/SalesAnalyst.geniespace.json"
    assert space["permissions"][0]["level"] == "CAN_RUN"


def test_examples_are_valid_read_only_sql(example):
    from dbde import connect
    space = example.spaces["SalesAnalyst"]
    for item in space.examples + space.benchmarks:
        sql = genie.example_sql(example, item, CATALOGS)
        connect.assert_read_only(sql)
        sqlglot.parse_one(sql, read="databricks")


def test_grants_cover_every_source(example):
    sql = genie.grants_sql(example, example.spaces["SalesAnalyst"])
    assert "GRANT SELECT ON TABLE ${gold_catalog}.sales.MetricNewBusiness" \
        in sql
    assert "GRANT SELECT ON TABLE ${gold_catalog}.sales.FactNewBusiness" \
        in sql
    assert "GRANT USE SCHEMA ON SCHEMA ${gold_catalog}.sales" in sql


def test_space_with_pii_table_is_refused(scratch):
    spec = scratch / "models" / "genie" / "SalesAnalyst.yml"
    data = yaml.safe_load(spec.read_text())
    data["tables"].append("distribution.DimAgent")
    spec.write_text(yaml.safe_dump(data, sort_keys=False))
    assert "A5" in _rules(load_project(scratch))


def test_pii_named_in_instructions_is_refused(scratch):
    spec = scratch / "models" / "genie" / "SalesAnalyst.yml"
    spec.write_text(spec.read_text().replace(
        "- Never name or list individual agents",
        "- Show AgentName when asked; never name or list individual agents"))
    assert "A8" in _rules(load_project(scratch))


def test_example_rules(scratch):
    spec = scratch / "models" / "genie" / "SalesAnalyst.yml"
    data = yaml.safe_load(spec.read_text())
    data["examples"] = [
        {"question": "Delete it", "query":
         "delete from {{ ref('sales.FactNewBusiness') }}"},
        {"question": "Other table", "query":
         "select * from {{ ref('policy_admin.Policy') }}"},
    ]
    spec.write_text(yaml.safe_dump(data, sort_keys=False))
    findings = [f for f in check.run_checks(load_project(scratch))
                if f.severity == "error"]
    rules = {f.rule for f in findings}
    assert {"A4", "A6"} <= rules
    assert any("not one of the space's tables" in f.message
               for f in findings)


def test_measure_rules(scratch):
    spec = scratch / "models" / "gold" / "sales" / "FactNewBusiness.yml"
    data = yaml.safe_load(spec.read_text())
    data["measures"].append({"name": "PremiumRow",
                             "expression": "AnnualPremiumAmount",
                             "description": "Not an aggregate.",
                             "synonyms": "Premium"})
    spec.write_text(yaml.safe_dump(data, sort_keys=False))
    assert {"A1", "A2"} <= _rules(load_project(scratch))


def test_space_needs_title_and_instructions(scratch):
    spec = scratch / "models" / "genie" / "SalesAnalyst.yml"
    data = yaml.safe_load(spec.read_text())
    del data["title"], data["instructions"]
    spec.write_text(yaml.safe_dump(data, sort_keys=False))
    assert "A3" in _rules(load_project(scratch))


def test_metric_view_deploys_after_its_tables(example):
    order = render.gold_deploy_order(example)
    view = order.index("gold/sales/metric_views/MetricNewBusiness.sql")
    assert view > order.index("gold/sales/tables/FactNewBusiness.sql")
    assert view > order.index("gold/distribution/tables/DimAgent.sql")
    assert order[-1] == "gold/ops/grants/SalesAnalyst.sql"
