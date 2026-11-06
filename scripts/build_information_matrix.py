#!/usr/bin/env python
"""Build the Information Matrix workbook from the dbde manifest.

Subcommands:
    profile  Run the read-only Databricks SQL in scripts/profiling/ on the
             gold catalog of a target, through its SQL warehouse. Saves
             data examples, hierarchy cardinality and BUS Matrix checks to
             target/matrix_profile.json.
    build    Fill a blank Information Matrix template from manifest.json
             (``python -m dbde parse --target <target>``), the profile
             file and the previous matrix. Writes the workbook plus a gap report (<out>.gaps.md) for
             people to complete.
    blank    Turn a filled matrix into a blank template.

The manifest has the shape of a dbt manifest, so this script is the
dbt kit's script with only the connection changed. Column
lineage comes from the specs (manifest ``lineage``), not from system tables.
Nothing here writes to Databricks. The same inputs always give the same
workbook. See .github/prompts/information-matrix.prompt.md and section 11 of
databricks_engineering_standards.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from copy import copy
from pathlib import Path
from string import Template

import openpyxl
import yaml
from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator
from openpyxl.worksheet.cell_range import CellRange

PROFILING_DIR = Path(__file__).parent / "profiling"
GLOSSARY_SEED = "business_glossary"

# Sheet -> (header row, columns that identify a row across matrix versions).
SHEETS = {
    "Glossary": (5, ["Concept"]),
    "EntitiesDimensions": (6, ["Entity (Dimension) Name"]),
    "Roleplaying Entities": (
        6, ["Entity (Dimension) Name", "Role-playing Name"]),
    "Attributes": (6, ["Entity (Dimension) Name", "Attribute Name"]),
    "Measure Groups": (6, ["Measure Group"]),
    "AttributeHierarchies": (
        6, ["Entity (Dimension) Name", "Hierarchy Name", "Attribute Name"]),
    "Measures": (6, ["Measure Group (Fact)", "Measure Label"]),
    "SilverLayerMapping": (3, ["Silver Table", "Column1"]),
    "BUS Matrix check": (5, ["fact_table", "fact_key", "dimension"]),
}
NO_VERSIONING = {"BUS Matrix check"}  # rebuilt in full on every run

VERSION_HEADERS = [
    "Matrix version when last updated", "Date last updated", "Update Status",
    "Change Type", "Change comments",
]
STATUS_HEADERS = [
    "IMPLEMENTATION: Implementation status", "Production status"]
NOISY_HEADERS = ["Data Example"]  # sampled, so not treated as a change
ID_PREFIX = {"dim#": "D", "mg#": "MG", "m#": "M"}  # other '#' columns: 1, 2..

PRESENTATION = {
    "data_lake": "Data Lake", "power_bi": "Power BI", "looker": "Looker",
    "managed_reports": "Managed Reports",
    "managed_dashboards": "Managed Dashboards",
}

# Columns a person must make sure are filled. Header -> where it comes from.
REQUIRED = {
    "EntitiesDimensions": {
        "Business Question Type": "meta.matrix.business_question",
        "SCD Type": "meta.matrix.scd_type",
        "Description": "model description",
    },
    "Attributes": {
        "Attribute Definition": "column description",
        "Data Example": "profile run, or column meta.matrix.example",
        "Logical Data Type": "column data_type",
        "Source Table": "profile run (column lineage)",
    },
    "Measure Groups": {
        "Definition": "model description",
        "Describe the Grain": "'Grain = ...' in the model description",
        "Required refresh/update frequency":
            "meta.matrix.refresh_frequency",
    },
    "Measures": {
        "Measure Definition": "comment on the measure in the mv_ model",
        "Additive/Non-Additive": "meta.matrix.measures.<name>.additivity",
        "Unit of measure": "meta.matrix.measures.<name>.unit",
    },
    "Glossary": {"Concept description": f"seeds/{GLOSSARY_SEED}.csv"},
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def norm(text) -> str:
    """Normalise a header so that 'Ready to Implement?' == 'ready to implement'."""
    return re.sub(r"\s+", " ", str(text)).strip().rstrip("?:").strip().lower()


def norm_val(value) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    return "" if value is None else str(value).strip().lower()


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def meta_of(obj: dict) -> dict:
    meta = dict(obj.get("meta") or {})
    meta.update((obj.get("config") or {}).get("meta") or {})
    return meta


def mx(obj: dict) -> dict:
    """The meta.matrix block of a model or column."""
    return meta_of(obj).get("matrix") or {}


def bare(relation: str | None) -> str:
    return (relation or "").replace("`", "").lower()


def quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def matrix_name(node: dict) -> str:
    """DimCustomer style name: meta.matrix.name, else derived from the model."""
    if mx(node).get("name"):
        return mx(node)["name"]
    prefix, _, rest = node["name"].partition("_")
    return {"dim": "Dim", "fct": "Fact"}.get(prefix, "") + pascal(rest)


def scd(value) -> str | None:
    if value in (None, ""):
        return None
    return f"Type {value}" if str(value).isdigit() else str(value)


_PII_FALSE = {"false", "no", "n", "0"}


def _pii_markers(model: dict, column: dict | None) -> list:
    """Every place a pii flag can be set: meta and databricks_tags, on the
    column and on the model."""
    markers = []
    for node in (column or {}, model):
        markers.append(meta_of(node).get("pii"))
        tags = (node.get("config") or {}).get("databricks_tags") or {}
        markers.append(tags.get("pii"))
    return [m for m in markers if m is not None]


def is_pii(model: dict, column: dict | None) -> bool:
    """Fail closed. A column is PII when the column or its model is marked pii
    in meta or databricks_tags, or when any marker is not a clear no (for
    example a typo). Only explicit false markers clear it. A column nobody
    marked is not PII here; the AGENTS.md rules require people to mark them."""
    return any(str(m).strip().lower() not in _PII_FALSE
               for m in _pii_markers(model, column))


def presentation(meta: dict) -> dict:
    chosen = set(meta.get("presentation") or [])
    return {header: "Yes" if key in chosen else None
            for key, header in PRESENTATION.items()} if chosen else {}


def classify(expr: str) -> tuple[str, str]:
    """(aggregation type, additivity) guessed from a measure expression."""
    text = re.sub(r"\s+", "", expr.lower())
    if text.startswith("count(distinct"):
        return "Distinct Count", "Non-Additive"
    for func, label, additive in [
            ("sum(", "Sum", "Additive"), ("count(", "Count", "Additive"),
            ("avg(", "Average", "Non-Additive"),
            ("min(", "Min", "Non-Additive"), ("max(", "Max", "Non-Additive")]:
        if text.startswith(func) and text.count("(") == 1:
            return label, additive
    return "Custom", "Non-Additive"


# --------------------------------------------------------------------------
# dbt project view
# --------------------------------------------------------------------------

class Project:
    """Read-only view over manifest.json, catalog.json and the profile."""

    def __init__(self, manifest, catalog=None, profile=None, project_dir="."):
        self.manifest = manifest
        self.catalog = (catalog or {}).get("nodes", {})
        self.profile = profile or {}
        self.project_dir = Path(project_dir)
        # Only this project's models. Installed packages (for example
        # dbt_project_evaluator) have models too; they are not part of the
        # matrix.
        root = (manifest.get("metadata") or {}).get("project_name")
        self.models = {uid: node for uid, node in manifest["nodes"].items()
                       if node["resource_type"] == "model"
                       and (not root or node.get("package_name") == root)}
        self.by_name = {node["name"]: uid for uid, node in self.models.items()}
        self.sources = manifest.get("sources", {})
        self.source_relations = {bare(s["relation_name"]): s
                                 for s in self.sources.values()}
        self.tests = defaultdict(list)
        for node in manifest["nodes"].values():
            if node["resource_type"] != "test" or not node.get("test_metadata"):
                continue
            uid = node.get("attached_node") or next(
                iter(node["depends_on"]["nodes"]), None)
            kwargs = node["test_metadata"].get("kwargs") or {}
            column = node.get("column_name") or kwargs.get("column_name")
            if uid and column:
                self.tests[(uid, column.lower())].append(node)
        self.lineage = defaultdict(set)
        for edge in (manifest.get("lineage", [])
                     + self.profile.get("lineage", [])):
            self.lineage[(edge["target_table"], edge["target_column"])].add(
                (edge["source_table"], edge["source_column"]))

    def prefixed(self, *prefixes) -> list[str]:
        return sorted((uid for uid, node in self.models.items()
                       if node["name"].startswith(prefixes)),
                      key=lambda uid: self.models[uid]["name"])

    def columns(self, uid) -> list[tuple[str, dict, str]]:
        """(name, column node, data type) from YAML, then catalog extras."""
        node = self.models[uid]
        cat = {k.lower(): v for k, v in
               (self.catalog.get(uid, {}).get("columns") or {}).items()}
        merged = {name.lower(): col
                  for name, col in (node.get("columns") or {}).items()}
        for name in cat:
            merged.setdefault(name, {"name": name})
        # Keys are lower-cased for matching; show the name as written.
        return [(col.get("name") or name, col, (
                    col.get("data_type")
                    or cat.get(name, {}).get("type") or "").lower())
                for name, col in merged.items() if not name.startswith("_")]

    def column_node(self, uid, name) -> dict:
        cols = self.models[uid].get("columns") or {}
        return next((c for k, c in cols.items() if k.lower() == name.lower()),
                    {})

    def relation(self, uid) -> str:
        return bare(self.models[uid].get("relation_name"))

    def target(self, uid) -> dict:
        node = self.models[uid]
        return {"Target Layer": "Gold" if node["name"].startswith(
                    ("dim_", "fct_", "mv_")) else "Silver",
                "Target Connection/Database":
                    f"{node['database']}.{node['schema']}",
                "Target Table": node.get("alias") or node["name"]}

    def upstream(self, uid, column) -> set[tuple[str, str]]:
        """Bronze (table, column) pairs feeding a column, via UC lineage."""
        todo, seen, found = [(self.relation(uid), column.lower())], set(), set()
        while todo:
            key = todo.pop()
            if key in seen:
                continue
            seen.add(key)
            if key[0] in self.source_relations:
                found.add(key)
            else:
                todo.extend(self.lineage.get(key, ()))
        return found

    def upstream_sources(self, uid) -> list[str]:
        """Table-level fallback: bronze relations reached through ref/source."""
        todo, seen, found = [uid], set(), set()
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            for dep in self.manifest["nodes"].get(current, {}).get(
                    "depends_on", {}).get("nodes", []):
                if dep in self.sources:
                    found.add(bare(self.sources[dep]["relation_name"]))
                else:
                    todo.append(dep)
        return sorted(found)

    def source_mapping(self, uid, columns) -> tuple[dict, str]:
        pairs = set()
        for column in columns:
            pairs |= self.upstream(uid, column)
        if pairs:
            tables, level = sorted({t for t, _ in pairs}), "column"
            cols = sorted(f"{t.rsplit('.', 1)[-1]}.{c}" for t, c in pairs)
        else:
            tables, level, cols = self.upstream_sources(uid), "table", []
        found = [self.source_relations[t] for t in tables
                 if t in self.source_relations]
        return {
            "Final Source": ", ".join(sorted({s["source_name"] for s in found})),
            "Single / Multiple Files":
                ("Single" if len(tables) == 1 else "Multiple") if tables
                else None,
            "Network / Storage Location": ", ".join(sorted(
                {f"{s['database']}.{s['schema']}" for s in found})),
            "Source Table": ", ".join(tables),
            "Source Columns": ", ".join(cols),
        }, level

    def relationships(self) -> list[tuple[str, str, str, str]]:
        """(child model, column, parent model, parent column) per test."""
        out = []
        for (uid, column), tests in self.tests.items():
            for test in tests:
                if test["test_metadata"]["name"] != "relationships":
                    continue
                parent = next((d for d in test["depends_on"]["nodes"]
                               if d != uid), None)
                if parent in self.models:
                    field = test["test_metadata"]["kwargs"].get("field") or ""
                    out.append((uid, column, parent, field.lower()))
        return sorted(set(out))

    def dq(self, uid, column, col) -> dict:
        rules = defaultdict(list)
        for constraint in col.get("constraints") or []:
            if constraint.get("type") == "not_null":
                rules["DQ: Completeness rules"].append("Not null")
            if constraint.get("type") == "primary_key":
                rules["DQ: Uniqueness rule"].append("Unique")
        for test in self.tests.get((uid, column.lower()), []):
            name = test["test_metadata"]["name"]
            kwargs = test["test_metadata"].get("kwargs") or {}
            if name == "unique":
                rules["DQ: Uniqueness rule"].append("Unique")
            elif name == "not_null":
                rules["DQ: Completeness rules"].append("Not null")
            elif name == "relationships":
                parent = next((d for d in test["depends_on"]["nodes"]
                               if d != uid), None)
                parent = matrix_name(self.models[parent]) \
                    if parent in self.models else kwargs.get("to")
                field = kwargs.get("field")
                rules["DQ: Integrity Rule"].append(
                    f"Must exist in {parent}.{field}")
                rules["Relationship Rules"].append(
                    f"M:1 to {parent} on {field}")
            elif name == "accepted_values":
                rules["DQ: Validity/Conformance rule"].append(
                    "One of: " + ", ".join(map(str, kwargs.get("values") or [])))
            elif name == "accepted_range":
                rules["DQ: Validity/Conformance rule"].append(
                    f"Between {kwargs.get('min_value')} and "
                    f"{kwargs.get('max_value')}")
            else:
                rules["DQ: Accuracy rule"].append(f"Test: {name}")
        return {k: "; ".join(dict.fromkeys(v)) for k, v in rules.items()}

    def metric_view(self, uid) -> dict:
        """Parse an mv_ model body (YAML with Jinja refs) into a dict."""
        raw = self.models[uid].get("raw_code") or ""
        raw = re.sub(r"\{#.*?#\}", "", raw, flags=re.S)
        raw = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", raw, flags=re.S)
        raw = re.sub(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
                     r"\1", raw)
        return yaml.safe_load(raw) or {}

    def examples(self, uid, column) -> list:
        return self.profile.get("examples", {}).get(uid, {}).get(column, [])

    def cardinality(self, uid, column):
        return self.profile.get("cardinality", {}).get(uid, {}).get(column)


# --------------------------------------------------------------------------
# Sheet builders: each returns a list of {header: value} rows
# --------------------------------------------------------------------------

def role_playing(p: Project) -> dict:
    """dim uid -> [(role name, column node)] when a dim has 2+ foreign keys."""
    names = defaultdict(dict)
    for child, column, parent, _ in p.relationships():
        if p.models[parent]["name"].startswith("dim_"):
            col = p.column_node(child, column)
            role = mx(col).get("role") or col.get("name") or column
            names[parent].setdefault(role, col)
    return {uid: sorted(roles.items()) for uid, roles in names.items()
            if len(roles) > 1}


def build_entities(p, roles):
    rows = []
    for uid in p.prefixed("dim_"):
        node, m = p.models[uid], mx(p.models[uid])
        rows.append({
            "Entity (Dimension) Name": matrix_name(node),
            "Role Playing (Yes/No)": "Yes" if uid in roles else "No",
            "Business Question Type": m.get("business_question"),
            "SCD Type": scd(m.get("scd_type")),
            "Description": node.get("description"),
            "Comments / Questions": m.get("comments"),
            **presentation(m),
            "IMPLEMENTATION: Implementation status": "Built in production",
            "Ready to Implement": m.get("ready"),
            "Implementation Priority": m.get("priority"),
            "Planned Release": m.get("planned_release"),
            **(m.get("extra") or {}),
        })
    return rows


def build_roles(p, roles):
    rows = []
    for uid, items in sorted(roles.items(),
                             key=lambda kv: p.models[kv[0]]["name"]):
        m = mx(p.models[uid])
        for role, col in items:
            rows.append({
                "Entity (Dimension) Name": matrix_name(p.models[uid]),
                "Role-playing Name": role,
                "Description": col.get("description"),
                **presentation(m),
            })
    return rows


def build_attributes(p, gaps):
    rows = []
    for uid in p.prefixed("dim_"):
        node, m = p.models[uid], mx(p.models[uid])
        for name, col, dtype in p.columns(uid):
            cm, pii = mx(col), is_pii(node, col)
            mapping, level = p.source_mapping(uid, [name])
            example = cm.get("example")
            if not example and not pii:
                example = "; ".join(map(str, p.examples(uid, name)))
            if pii and not cm.get("example"):
                gaps.append(f"Attributes: {node['name']}.{name} is PII; add a "
                            "synthetic `meta.matrix.example`.")
            if level == "table" and p.profile:
                gaps.append(f"Attributes: {node['name']}.{name} has no column "
                            "lineage; source columns need a person.")
            rows.append({
                "Entity (Dimension) Name": matrix_name(node),
                "Attribute Name": name,
                "Attribute Definition": col.get("description"),
                "Comments / Questions": cm.get("comments"),
                "Data Example": example,
                "Logical Data Type": dtype,
                "Attribute SCD": scd(cm.get("scd") or m.get("scd_type")),
                "Scope rules": cm.get("scope_rule"),
                **p.dq(uid, name, col),
                **presentation(m),
                **mapping,
                "Value calculation rule": cm.get("value_rule"),
                **p.target(uid),
                "Target Column(s)": name,
                "IMPLEMENTATION: Implementation status": "Built in production",
                **(cm.get("extra") or {}),
            })
    return rows


def build_measure_groups(p):
    rows = []
    for uid in p.prefixed("fct_"):
        node, m = p.models[uid], mx(p.models[uid])
        grain = m.get("grain")
        if not grain:
            found = re.search(r"grain\s*[=:]\s*([^.\n]+)",
                              node.get("description") or "", re.I)
            grain = found.group(1).strip() if found else None
        rows.append({
            "Measure Group": matrix_name(node),
            "Definition": node.get("description"),
            "Genie Description": m.get("genie_description"),
            "Describe the Grain": grain,
            "Transaction/Event frequency": m.get("event_frequency"),
            "Required refresh/update frequency": m.get("refresh_frequency"),
            "Measure Group Type": m.get("group_type", "Physical"),
            "Comments / Questions": m.get("comments"),
            **presentation(m),
            "IMPLEMENTATION: Implementation status": "Built in production",
            "Ready to Implement": m.get("ready"),
            "Implementation Priority": m.get("priority"),
            "Planned Release": m.get("planned_release"),
            **(m.get("extra") or {}),
        })
    return rows


def build_measures(p, gaps):
    rows = []
    for uid in p.prefixed("mv_"):
        node = p.models[uid]
        spec = p.metric_view(uid)
        fact_uid = p.by_name.get(str(spec.get("source", "")).rsplit(".", 1)[-1])
        group = matrix_name(p.models[fact_uid]) if fact_uid else node["name"]
        fact_cols = {name for name, _, _ in p.columns(fact_uid)} \
            if fact_uid else set()
        settings = mx(node).get("measures") or {}
        for measure in spec.get("measures") or []:
            mm = settings.get(measure["name"]) or {}
            expr = str(measure.get("expr", ""))
            aggregation, additivity = classify(expr)
            if aggregation == "Custom" and not mm.get("additivity"):
                gaps.append(f"Measures: {node['name']}.{measure['name']} has "
                            "a custom expression; confirm additivity.")
            used = sorted({t.lower() for t in re.findall(r"[A-Za-z_]\w*", expr)}
                          & fact_cols)
            mapping, _ = p.source_mapping(fact_uid, used) if fact_uid \
                else ({}, None)
            scenarios = {s.lower() for s in mm.get("scenarios", ["actual"])}
            rows.append({
                "Measure Group (Fact)": group,
                "Measure Label": measure["name"],
                "Measure Definition": measure.get("comment"),
                "Measure Example": mm.get("example"),
                "Comments / Questions": mm.get("comments"),
                "Business priority": mm.get("priority"),
                "Additive/Non-Additive": mm.get("additivity", additivity),
                "Aggregation Measure Type": mm.get("aggregation", aggregation),
                "Unit of measure": mm.get("unit"),
                "Scope rules": mm.get("scope_rule"),
                "Actual": "Yes" if "actual" in scenarios else None,
                "Budget": "Yes" if "budget" in scenarios else None,
                "Forecast": "Yes" if "forecast" in scenarios else None,
                "Industry Average Benchmark":
                    "Yes" if "benchmark" in scenarios else None,
                **mapping,
                "Value calculation rule": expr,
                **p.target(uid),
                "Target Column(s)": measure["name"],
                "IMPLEMENTATION: Implementation status": "Built in production",
                **(mm.get("extra") or {}),
            })
    return rows


def build_hierarchies(p):
    rows = []
    for uid in p.prefixed("dim_"):
        node = p.models[uid]
        for name, col, _ in p.columns(uid):
            level = mx(col).get("hierarchy")
            if not level:
                continue
            rows.append({
                "Entity (Dimension) Name": matrix_name(node),
                "Hierarchy Name": level.get("name"),
                "Attribute Name": name,
                "Hierarchy Level": level.get("level"),
                "Cardinality": p.cardinality(uid, name),
                "IMPLEMENTATION: Implementation status": "Built in production",
            })
    return sorted(rows, key=lambda r: (r["Entity (Dimension) Name"],
                                       str(r["Hierarchy Name"]),
                                       r["Hierarchy Level"] or 0))


def build_glossary(p):
    seed = next((n for n in p.manifest["nodes"].values()
                 if n["resource_type"] == "seed"
                 and n["name"] == GLOSSARY_SEED), None)
    if not seed:
        return []
    path = p.project_dir / seed["original_file_path"]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{
            "Glossary Category": rec.get("category"),
            "Concept": rec.get("concept"),
            "Corresponding Acronym": rec.get("acronym"),
            "Context": rec.get("context"),
            "Concept description": rec.get("description"),
        } for rec in csv.DictReader(handle)]


def build_silver(p):
    rows = []
    for uid in p.prefixed("stg_", "int_"):
        node = p.models[uid]
        config = node.get("config") or {}
        load = config.get("materialized")
        if load == "incremental":
            load += f" ({config.get('incremental_strategy') or 'default'})"
        for name, col, dtype in p.columns(uid):
            pairs = p.upstream(uid, name)
            tables = sorted({t for t, _ in pairs}) or p.upstream_sources(uid)
            rows.append({
                "Bronze Table": ", ".join(tables),
                "Bronze Column": ", ".join(sorted({c for _, c in pairs})),
                "Silver Table": node.get("alias") or node["name"],
                "Column1": name,
                "LoadType": load,
                "DataType": dtype,
                "Business Rules": col.get("description"),
                "Production status": "Built in production",
            })
    return rows


BUS_CHECK_HEADERS = [
    "check_no", "fact_table", "fact_key", "dimension", "dimension_key",
    "dim_rows", "dim_duplicate_keys", "fact_rows", "fact_rows_null_key",
    "fact_distinct_keys", "fact_rows_no_match", "distinct_keys_no_match",
    "sample_keys_no_match"]


def ensure_bus_check_sheet(wb):
    """Add the 'BUS Matrix check' sheet when the template does not have it."""
    if "BUS Matrix check" in wb.sheetnames:
        return
    ws = wb.create_sheet("BUS Matrix check")
    ws["A1"] = ("BUS Matrix check: are the fact to dimension links 1:M "
                "(or 1:1)?")
    ws["A3"] = ("How to read: dim_duplicate_keys = 0 means the dimension key "
                "is unique (the '1' side holds). fact_rows_no_match counts "
                "fact rows whose key has no dimension row. "
                "fact_rows_null_key counts fact rows with no key.")
    for col, header in enumerate(BUS_CHECK_HEADERS, 1):
        ws.cell(5, col).value = header
        ws.cell(5, col).font = openpyxl.styles.Font(bold=True)
        ws.column_dimensions[ws.cell(5, col).column_letter].width = 22


def build_bus_checks(p):
    return [dict(check) for check in p.profile.get("bus_checks", [])]


# --------------------------------------------------------------------------
# Versioning against the previous matrix
# --------------------------------------------------------------------------

def header_map(ws, header_row) -> dict:
    return {norm(c.value): c.column for c in ws[header_row]
            if not isinstance(c, MergedCell) and c.value not in (None, "")}


def resolve(headers: dict, key: str):
    key = norm(key)
    if key in headers:
        return headers[key]
    hits = [col for header, col in headers.items() if header.startswith(key)]
    return hits[0] if len(hits) == 1 else None


def row_key(row: dict, keys) -> tuple:
    return tuple(norm_val(row.get(norm(k))) for k in keys)


def read_previous(path) -> dict:
    """sheet -> {row key: {normalised header: value}} from the last matrix."""
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {}
    for sheet, (header_row, keys) in SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        headers = header_map(ws, header_row)
        rows = {}
        for r in range(header_row + 1, ws.max_row + 1):
            rec = {h: ws.cell(r, c).value for h, c in headers.items()}
            key = row_key(rec, keys)
            if any(key):
                rows[key] = rec
        out[sheet] = rows
    return out


def canonical(ws, header_row, rows, warnings) -> list[dict]:
    """Re-key rows by the template's normalised header text."""
    headers = header_map(ws, header_row)
    by_col = {c: h for h, c in headers.items()}
    out = []
    for row in rows:
        new = {}
        for key, value in row.items():
            if value in (None, "", []):
                continue
            col = resolve(headers, key)
            if col is None:
                warnings.add(f"{ws.title}: no column matches '{key}'")
            else:
                new[by_col[col]] = value
        out.append(new)
    return out


def apply_versioning(sheet, rows, previous, version, today):
    """Carry human-entered values forward and fill the version columns."""
    _, keys = SHEETS[sheet]
    old_rows = dict(previous.get(sheet, {}))
    managed = {norm(h) for h in VERSION_HEADERS}
    ignore = managed | {norm(h) for h in NOISY_HEADERS}
    stamp = {norm("Matrix version when last updated"): version,
             norm("Date last updated"): today}
    out = []
    for row in rows:
        new = dict(row)
        old = old_rows.pop(row_key(new, keys), None)
        if old is None:
            new.update(stamp, **{norm("Update Status"): "Generated: review",
                                 norm("Change Type"): "Added"})
        else:
            changed = sorted(h for h, v in new.items() if h not in ignore
                             and not h.endswith("#")
                             and norm_val(v) != norm_val(old.get(h)))
            for header, value in old.items():
                if header not in new and value not in (None, "") \
                        and (header not in managed or not changed):
                    new[header] = value
            if changed:
                new.update(stamp, **{
                    norm("Update Status"): "Generated: review",
                    norm("Change Type"): "Updated",
                    norm("Change comments"): "Changed: " + ", ".join(changed)})
        out.append(new)
    for old in old_rows.values():
        if norm_val(old.get(norm("Change Type"))) == "removed":
            out.append(old)
            continue
        removed = {h: v for h, v in old.items() if v not in (None, "")}
        removed.update(stamp, **{norm("Update Status"): "Generated: review",
                                 norm("Change Type"): "Removed"})
        for header in STATUS_HEADERS:
            if norm(header) in old:
                removed[norm(header)] = "Removed from production"
        out.append(removed)
    return out


def find_gaps(sheet, rows) -> list[str]:
    gaps = []
    keys = SHEETS[sheet][1]
    for header, hint in REQUIRED.get(sheet, {}).items():
        missing = [" / ".join(str(row.get(norm(k)) or "") for k in keys)
                   for row in rows
                   if not any(norm_val(v) for h, v in row.items()
                              if h.startswith(norm(header)))
                   and norm_val(row.get(norm("Change Type"))) != "removed"]
        if missing:
            gaps.append(f"{sheet}: '{header}' is empty for {len(missing)} "
                        f"row(s), set via {hint}: " + "; ".join(missing[:15])
                        + (" ..." if len(missing) > 15 else ""))
    return gaps


# --------------------------------------------------------------------------
# Writing the workbook
# --------------------------------------------------------------------------

def cell_value(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value)
    return value


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def set_value(cell, value):
    """Write data or metadata into a cell as data, never as a formula.

    Text that starts with = + - @ tab or CR is stored as a string with a quote
    prefix, so Excel will not evaluate it. Only formulas copied from the
    template prototype row stay formulas.
    """
    cell.value = value
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        cell.data_type = "s"
        cell.quotePrefix = True


def write_rows(ws, header_row, rows, warnings):
    """Clear the data rows under header_row and write rows in their place.

    The first data row of the template is the prototype: its styles and
    formulas are copied to every new row. ID columns (headers ending in '#')
    get static IDs, because the template's ID formulas differ per row.
    """
    headers = header_map(ws, header_row)
    if not headers:
        warnings.add(f"{ws.title}: no header row at row {header_row}")
        return
    first, width = header_row + 1, max(headers.values())
    proto = {c: (ws.cell(first, c).value, copy(ws.cell(first, c)._style))
             for c in range(1, width + 1)}
    id_cols = {c: ID_PREFIX.get(h, "") for h, c in headers.items()
               if h.endswith("#")}
    for r in range(first, max(ws.max_row, first) + 1):
        for c in range(1, width + 1):
            cell = ws.cell(r, c)
            if not isinstance(cell, MergedCell):
                cell.value = None
    for i, row in enumerate(rows or [{}]):  # empty table keeps one row
        r = first + i
        for c, (value, style) in proto.items():
            cell = ws.cell(r, c)
            cell._style = copy(style)
            if c in id_cols and rows:
                cell.value = f"{id_cols[c]}{i + 1}" if id_cols[c] else i + 1
            elif isinstance(value, str) and value.startswith("="):
                cell.value = Translator(
                    value, origin=ws.cell(first, c).coordinate
                ).translate_formula(cell.coordinate)
        for key, value in row.items():
            col = resolve(headers, key)
            if col is None:
                if key not in {norm(h) for h in VERSION_HEADERS}:
                    warnings.add(f"{ws.title}: no column matches '{key}'")
            elif col not in id_cols and value not in (None, ""):
                set_value(ws.cell(r, col), cell_value(value))
    last = first + max(len(rows), 1) - 1
    for name in list(ws.tables):
        table = ws.tables[name]
        ref = CellRange(table.ref)
        if ref.min_row == header_row:
            ref.max_row = last
            table.ref = ref.coord
            if table.autoFilter is not None:
                table.autoFilter.ref = ref.coord
    for validation in ws.data_validations.dataValidation:
        for cell_range in validation.sqref.ranges:
            if cell_range.min_row <= first + 1 <= cell_range.max_row + 1 \
                    and cell_range.max_row < last:
                cell_range.max_row = last


def write_bus_matrix(ws, p, roles, warnings):
    """Fill 'BUS Matrix-current': dims down, facts across, 1:M where joined."""
    internal = next((c.column for c in ws[6]
                     if norm(c.value or "") == "internal use"), 26)
    slots, first = list(range(5, internal - 1)), 8
    dims, facts = p.prefixed("dim_"), p.prefixed("fct_")
    links = {(parent, child) for child, _, parent, _ in p.relationships()}
    for r in range(6, max(ws.max_row, first) + 1):
        for c in [1, 2, 3, *slots] if r >= first else slots:
            if not isinstance(ws.cell(r, c), MergedCell):
                ws.cell(r, c).value = None
    if len(facts) > len(slots):
        warnings.add(f"BUS Matrix-current: {len(facts)} facts, only "
                     f"{len(slots)} columns in the template")
    for i, fact in enumerate(facts[:len(slots)], 1):
        set_value(ws.cell(6, slots[i - 1]), matrix_name(p.models[fact]))
        ws.cell(7, slots[i - 1]).value = f"MG{i}"
    for j, dim in enumerate(dims, 1):
        r = first + j - 1
        ws.cell(r, 1).value = f"D{j}"
        set_value(ws.cell(r, 2), matrix_name(p.models[dim]))
        set_value(ws.cell(r, 3),
                  ", ".join(role for role, _ in roles.get(dim, [])))
        for i, fact in enumerate(facts[:len(slots)]):
            if (dim, fact) in links:
                ws.cell(r, slots[i]).value = "1:M"


def history_rows(ws) -> tuple[int | None, list[list]]:
    """(header row, existing rows) of the Doc Controls version history."""
    header = next((r for r in range(1, 40)
                   if norm(ws.cell(r, 1).value or "") == "date"
                   and norm(ws.cell(r, 2).value or "") == "version"), None)
    rows, r = [], (header or 0) + 1
    while header and ws.cell(r, 1).value not in (None, ""):
        rows.append([ws.cell(r, c).value for c in range(1, 5)])
        r += 1
    return header, rows


def write_history(ws, rows):
    header, existing = history_rows(ws)
    if header is None:
        return
    for r in range(header + 1, header + 1 + max(len(existing), len(rows))):
        for c in range(1, 5):
            ws.cell(r, c).value = None
    for i, values in enumerate(rows):
        for c, value in enumerate(values, 1):
            set_value(ws.cell(header + 1 + i, c), value)


def fix_external_links(wb):
    """v1.1 points sheet titles at '[1]Doc Controls' (another file)."""
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=1, max_row=5, max_col=3):
            for cell in row:
                if isinstance(cell.value, str) and "[1]Doc Controls" in cell.value:
                    cell.value = cell.value.replace(
                        "'[1]Doc Controls'", "'Doc Controls'")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def connect(target):
    """SQL warehouse connection for a target (read-only use).

    Uses the dbde toolkit's sign-in (``dbde.connect``): the service
    principal in DATABRICKS_HOST / DATABRICKS_CLIENT_ID /
    DATABRICKS_CLIENT_SECRET when set, otherwise the target's CLI profile.
    Against production, use only the read-only matrix service principal.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dbde.connect import sql_connection
    from dbde.project import load_config

    return sql_connection(load_config(Path.cwd()), target)


def render(name, **params) -> str:
    text = (PROFILING_DIR / f"{name}.sql").read_text(encoding="utf-8")
    return Template(text).substitute(**params)


def cmd_profile(args):
    manifest = load_json(args.manifest)
    p = Project(manifest)
    result = {
        "profiled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "manifest_generated_at": manifest["metadata"].get("generated_at"),
        "lineage": [], "examples": {}, "cardinality": {}, "bus_checks": [],
        "skipped_pii": [],
    }
    with connect(args.target) as conn:
        cursor = conn.cursor()

        def run(sql_text):
            cursor.execute(sql_text)
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

        for uid in ([] if args.bus_only else p.prefixed("dim_")):
            node = p.models[uid]
            for name, col, _ in p.columns(uid):
                if is_pii(node, col):
                    result["skipped_pii"].append(f"{node['name']}.{name}")
                elif not mx(col).get("example"):
                    result["examples"].setdefault(uid, {})[name] = [
                        r["example_value"] for r in run(render(
                            "column_examples", relation=node["relation_name"],
                            column=quote(name), sample_rows=args.sample_rows,
                            max_examples=3))]
                if mx(col).get("hierarchy"):
                    result["cardinality"].setdefault(uid, {})[name] = run(
                        render("column_cardinality",
                               relation=node["relation_name"],
                               column=quote(name)))[0]["distinct_values"]
        if not args.skip_bus_check:
            checks = [r for r in p.relationships()
                      if p.models[r[0]]["name"].startswith("fct_")
                      and p.models[r[2]]["name"].startswith("dim_")]
            for i, (fact, column, dim, field) in enumerate(checks, 1):
                fact_node = p.models[fact]
                col = p.column_node(fact, column)
                stats = run(render(
                    "bus_matrix_check",
                    fact_relation=fact_node["relation_name"],
                    fact_key=quote(column),
                    dim_relation=p.models[dim]["relation_name"],
                    dim_key=quote(field),
                    sample_limit=0 if (
                        is_pii(fact_node, col)
                        or is_pii(p.models[dim], p.column_node(dim, field))
                    ) else 5))[0]
                result["bus_checks"].append({
                    "check_no": i, "fact_table": p.relation(fact),
                    "fact_key": column,
                    "dimension": matrix_name(p.models[dim]),
                    "dimension_key": f"{p.relation(dim)}.{field}", **stats})
    Path(args.out).write_text(json.dumps(result, indent=2, default=str),
                              encoding="utf-8")
    print(f"Profile written to {args.out}")


def cmd_build(args):
    manifest = load_json(args.manifest)
    catalog = load_json(args.catalog) if args.catalog and \
        Path(args.catalog).exists() else None
    profile = load_json(args.profile) if args.profile and \
        Path(args.profile).exists() else None
    p = Project(manifest, catalog, profile, args.project_dir)
    previous = read_previous(args.previous) if args.previous else {}
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    gaps, warnings = [], set()
    if not profile:
        gaps.append("No profile file: Data Example, source columns, "
                    "cardinality and the BUS Matrix check are empty. Run "
                    "`build_information_matrix.py profile` first. Source columns still "
                    "come from the spec lineage.")
    roles = role_playing(p)
    sheets = {
        "Glossary": build_glossary(p),
        "EntitiesDimensions": build_entities(p, roles),
        "Roleplaying Entities": build_roles(p, roles),
        "Attributes": build_attributes(p, gaps),
        "Measure Groups": build_measure_groups(p),
        "AttributeHierarchies": build_hierarchies(p),
        "Measures": build_measures(p, gaps),
        "SilverLayerMapping": build_silver(p),
        "BUS Matrix check": build_bus_checks(p),
    }
    wb = openpyxl.load_workbook(args.template)
    fix_external_links(wb)
    if profile and profile.get("bus_checks"):
        ensure_bus_check_sheet(wb)
    counts = {}
    for sheet, rows in sheets.items():
        if sheet not in wb.sheetnames:
            warnings.add(f"Template has no '{sheet}' sheet")
            continue
        rows = canonical(wb[sheet], SHEETS[sheet][0], rows, warnings)
        if sheet not in NO_VERSIONING:
            rows = apply_versioning(sheet, rows, previous, args.version, today)
            gaps.extend(find_gaps(sheet, rows))
        write_rows(wb[sheet], SHEETS[sheet][0], rows, warnings)
        counts[sheet] = len(rows)
    if "BUS Matrix-current" in wb.sheetnames:
        write_bus_matrix(wb["BUS Matrix-current"], p, roles, warnings)
        wb["BUS Matrix-current"].sheet_state = "visible"
    if "BUS Matrix check" in wb.sheetnames and profile:
        wb["BUS Matrix check"]["A2"] = (
            f"Run on {profile.get('profiled_at', '')[:10]} against the "
            "gold catalog, by build_information_matrix.py "
            "profile. Read-only counts.")
        for check in profile.get("bus_checks", []):
            if check.get("dim_duplicate_keys") or check.get("fact_rows_no_match"):
                gaps.append(
                    f"BUS Matrix check: {check['fact_table']}.{check['fact_key']}"
                    f" -> {check['dimension']}: {check.get('dim_duplicate_keys')}"
                    f" duplicate dim keys, {check.get('fact_rows_no_match')} "
                    "unmatched fact rows.")
    if "Doc Controls" in wb.sheetnames:
        history = []
        if args.previous:
            old = openpyxl.load_workbook(args.previous, data_only=True)
            if "Doc Controls" in old.sheetnames:
                history = history_rows(old["Doc Controls"])[1]
        history.append([
            today.strftime("%d %b %Y"), args.version, args.author,
            "Generated from manifest "
            f"{manifest['metadata'].get('invocation_id', '')} "
            f"({manifest['metadata'].get('generated_at', '')[:19]})."])
        write_history(wb["Doc Controls"], history)
    wb.calculation.fullCalcOnLoad = True
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    report = out.with_suffix(".gaps.md")
    report.write_text(gap_report(args, counts, gaps, warnings, profile),
                      encoding="utf-8")
    print(f"Workbook: {out}\nGap report: {report} "
          f"({len(gaps)} gaps, {len(warnings)} warnings)")


def gap_report(args, counts, gaps, warnings, profile) -> str:
    lines = [f"# Information Matrix {args.version}: gap report", "",
             f"Built {dt.date.today().isoformat()} from `{args.manifest}`.", "",
             "| Sheet | Rows |", "| --- | --- |"]
    lines += [f"| {sheet} | {n} |" for sheet, n in counts.items()]
    lines += ["", "## Needs a person", ""]
    lines += [f"- {gap}" for gap in gaps] or ["- Nothing."]
    if profile and profile.get("skipped_pii"):
        lines += ["", "## PII columns not sampled", ""]
        lines += [f"- {c}" for c in profile["skipped_pii"]]
    if warnings:
        lines += ["", "## Template warnings", ""]
        lines += [f"- {w}" for w in sorted(warnings)]
    lines += ["", "Not generated: 'BUS Matrix' (legacy layout), 'Usage Guide',"
              " and any working sheets (e.g. 'Measure examples WIP').", ""]
    return "\n".join(lines)


def cmd_blank(args):
    wb = openpyxl.load_workbook(args.source)
    fix_external_links(wb)
    warnings = set()
    for sheet, (header_row, _) in SHEETS.items():
        if sheet in wb.sheetnames:
            write_rows(wb[sheet], header_row, [], warnings)
    if "BUS Matrix-current" in wb.sheetnames:
        write_bus_matrix(wb["BUS Matrix-current"],
                         Project({"nodes": {}, "sources": {}}), {}, warnings)
    if "Doc Controls" in wb.sheetnames:
        write_history(wb["Doc Controls"], [])
    wb.calculation.fullCalcOnLoad = True
    wb.save(args.out)
    print(f"Blank template: {args.out}")
    for warning in sorted(warnings):
        print(f"warning: {warning}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    prof = sub.add_parser("profile", help="query Databricks (read-only)")
    prof.add_argument("--manifest", default="target/manifest.json")
    prof.add_argument("--out", default="target/matrix_profile.json")
    prof.add_argument("--sample-rows", type=int, default=10000)
    prof.add_argument("--skip-bus-check", action="store_true")
    prof.add_argument("--bus-only", action="store_true",
                      help="run only the BUS Matrix check: counts, no data "
                           "examples")
    prof.add_argument("--target", default="dev",
                      help="dbde.yml target; production needs the read-only "
                           "matrix service principal")
    prof.set_defaults(func=cmd_profile)

    build = sub.add_parser("build", help="fill the template")
    build.add_argument("--manifest", default="target/manifest.json")
    build.add_argument("--catalog", default=None,
                       help="optional dbt-style catalog.json (not needed: "
                            "the manifest carries data types)")
    build.add_argument("--profile", default="target/matrix_profile.json")
    build.add_argument("--template",
                       default="docs/information_matrix_template.xlsx")
    build.add_argument("--previous", help="last published matrix (.xlsx)")
    build.add_argument("--version", required=True, help="e.g. '1.2 Production'")
    build.add_argument("--author", required=True)
    build.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    build.add_argument("--project-dir", default=".")
    build.add_argument("--out", required=True)
    build.set_defaults(func=cmd_build)

    blank = sub.add_parser("blank", help="make a blank template")
    blank.add_argument("--source", required=True)
    blank.add_argument("--out", required=True)
    blank.set_defaults(func=cmd_blank)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
