"""Write ``target/manifest.json`` in the shape of a dbt manifest.

The shape is deliberately dbt's (nodes, sources, tests with
``test_metadata``, ``depends_on``), so tools written for dbt artifacts, such
as ``scripts/build_information_matrix.py``, work on this project unchanged.

Relation names use the catalogs of one target (``python -m dbde parse
--target prod`` for the production matrix; ``dev`` by default).

Names follow dbt's prefixes so those tools can tell layers apart; the real
table name is the ``alias``:

| Spec | Manifest name | alias |
| --- | --- | --- |
| silver source ``crm.Customer`` | ``stg_crm__customer`` | ``Customer`` |
| silver integrated ``sales.Agent`` | ``int_sales__agent`` | ``Agent`` |
| gold ``sales.DimAgent`` | ``dim_agent`` | ``DimAgent`` |
| gold ``sales.FactNewBusiness`` | ``fct_new_business`` | ``FactNewBusiness`` |
| metric view of a fact | ``mv_new_business`` | ``MetricNewBusiness`` |

Column lineage (bronze -> silver -> gold) is written to ``lineage``. It
comes from silver expressions, explicit ``lineage:`` entries and, failing
those, same-named columns in the tables a model reads.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Dict, List

import yaml

from dbde import dq
from dbde.project import Model, Project, identifiers, snake

GLOSSARY = "reference/business_glossary.csv"


def manifest_name(model: Model) -> str:
    """Return the dbt-style name of a model.

    Args:
        model: A silver or gold model.

    Returns:
        e.g. ``stg_crm__customer`` or ``dim_agent``.
    """
    if model.layer == "silver":
        prefix = "stg" if model.kind == "source" else "int"
        return f"{prefix}_{model.schema}__{snake(model.name)}"
    for word, prefix in (("Dim", "dim"), ("Fact", "fct"), ("Bridge", "brg")):
        if model.name.startswith(word):
            return f"{prefix}_{snake(model.name[len(word):])}"
    return snake(model.name)


def _uid(project: Project, model: Model) -> str:
    """Return the manifest unique id of a model.

    Args:
        project: The loaded project.
        model: The model.

    Returns:
        ``model.<project>.<name>``.
    """
    return f"model.{project.config.project}.{manifest_name(model)}"


def _source_uid(project: Project, source: str, table: str) -> str:
    """Return the manifest unique id of a bronze table.

    Args:
        project: The loaded project.
        source: Source name.
        table: Table name.

    Returns:
        ``source.<project>.<source>.<table>``.
    """
    return f"source.{project.config.project}.{source}.{table}"


def _relation(project: Project, model: Model, catalogs: Dict[str, str]) -> str:
    """Return the three-part name of a model in a target's catalogs.

    Args:
        project: The loaded project.
        model: The model.
        catalogs: Layer -> catalog of the target.

    Returns:
        ``catalog.schema.Table``.
    """
    return project.relation(model, catalogs)


def _columns(model: Model) -> Dict[str, Dict[str, Any]]:
    """Return the manifest columns of a model (audit columns left out).

    Args:
        model: The model.

    Returns:
        Column name -> column node.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if model.layer == "gold":
        out[model.surrogate_key] = {
            "name": model.surrogate_key, "data_type": "bigint", "meta": {},
            "description": f"Surrogate key of {model.name}.",
            "constraints": [{"type": "not_null"}, {"type": "primary_key"}]}
    for col in model.columns:
        out[col.name] = {
            "name": col.name, "data_type": col.data_type,
            "description": col.description or "", "meta": col.meta,
            "constraints": [{"type": "not_null"}] if col.business_key else []}
    if model.is_scd2:
        for name, data_type, text in (
                ("StartDate", "date", "First date this version is valid."),
                ("EndDate", "date", "Last date this version is valid "
                 "(9999-12-31 for the current version)."),
                ("IsCurrent", "boolean", "True for the current version.")):
            out[name] = {"name": name, "data_type": data_type,
                         "description": text, "meta": {}, "constraints": []}
    return out


def _test_node(project: Project, test: dq.Test) -> Dict[str, Any]:
    """Return a dbt-style test node.

    Args:
        project: The loaded project.
        test: The test.

    Returns:
        The node.
    """
    if test.layer == "source":
        source, table = test.table.split(".", 1)
        attached = _source_uid(project, source, table)
    else:
        attached = _uid(project, project.models[test.table])
    depends = [attached]
    kwargs: Dict[str, Any] = {"column_name": test.column, **test.args}
    if test.kind in ("relationships", "reconciles_to"):
        other = project.models.get(str(test.args.get("to")
                                       or test.args.get("compare_to")))
        if other:
            depends.append(_uid(project, other))
            kwargs["to"] = f"ref('{manifest_name(other)}')"
    name = f"{test.kind}_{test.table.replace('.', '_')}_{test.name}"
    return {
        "unique_id": f"test.{project.config.project}.{name}",
        "resource_type": "test", "name": name,
        "package_name": project.config.project,
        "column_name": test.column, "attached_node": attached,
        "test_metadata": {"name": test.kind, "kwargs": kwargs},
        "depends_on": {"nodes": depends},
        "config": {"severity": test.severity},
        "dbde": {"layer": test.layer, "table": test.table,
                 "implied": test.implied},
    }


def lineage(project: Project,
            catalogs: Dict[str, str]) -> List[Dict[str, str]]:
    """Return column lineage edges, lower-cased like the matrix expects.

    Args:
        project: The loaded project.
        catalogs: Layer -> catalog of the target.

    Returns:
        Edges with ``source_table``, ``source_column``, ``target_table``,
        ``target_column``.
    """
    edges = []

    def edge(src_rel: str, src_col: str, tgt_rel: str, tgt_col: str) -> None:
        edges.append({"source_table": src_rel.lower(),
                      "source_column": src_col.lower(),
                      "target_table": tgt_rel.lower(),
                      "target_column": tgt_col.lower()})

    for model in project.silver() + project.gold():
        target = _relation(project, model, catalogs)
        if model.source:
            source, table = model.source
            bronze = project.sources.get(source)
            if not bronze or table not in bronze.tables:
                continue
            src_rel = project.source_relation(source, table, catalogs)
            known = {c.lower() for c in bronze.tables[table].columns}
            for col in model.columns:
                for token in dict.fromkeys(identifiers(str(col.expression
                                                           or col.name))):
                    if token in known:
                        edge(src_rel, token, target, col.name)
            continue
        inputs = [project.models[r] for r in model.refs
                  if r in project.models]
        for col in model.columns:
            if col.lineage:
                for item in col.lineage:
                    parts = str(item).split(".")
                    if len(parts) == 3 and ".".join(parts[:2]) in \
                            project.models:
                        edge(_relation(project,
                                       project.models[".".join(parts[:2])],
                                       catalogs),
                             parts[2], target, col.name)
                continue
            for parent in inputs:
                if any(c.name.lower() == col.name.lower()
                       for c in parent.columns):
                    edge(_relation(project, parent, catalogs), col.name,
                         target, col.name)
    return edges


def build(project: Project, target: str = "dev") -> Dict[str, Any]:
    """Build the manifest.

    Args:
        project: The loaded project.
        target: Target whose catalogs the relation names use.

    Returns:
        The manifest as a dict.
    """
    catalogs = project.config.catalogs(target)
    name = project.config.project
    nodes: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}
    for source in project.sources.values():
        for table in source.tables.values():
            uid = _source_uid(project, source.name, table.name)
            sources[uid] = {
                "unique_id": uid, "resource_type": "source",
                "source_name": source.name, "name": table.name,
                "database": catalogs["bronze"],
                "schema": source.schema,
                "relation_name": project.source_relation(
                    source.name, table.name, catalogs),
                "description": table.description or "",
                "source_description": source.description or "",
                "columns": {c: {"name": c, "data_type": t}
                            for c, t in table.columns.items()},
                "freshness": source.freshness, "meta": table.meta,
            }
    for model in project.silver() + project.gold():
        uid = _uid(project, model)
        depends = [_uid(project, project.models[r]) for r in model.refs
                   if r in project.models]
        if model.source:
            depends.append(_source_uid(project, *model.source))
        incremental = model.load.get("mode") == "incremental" or \
            model.layer == "silver"
        nodes[uid] = {
            "unique_id": uid, "resource_type": "model", "package_name": name,
            "name": manifest_name(model), "alias": model.name,
            "database": catalogs[model.layer],
            "schema": model.db_schema or model.schema,
            "relation_name": _relation(project, model, catalogs),
            "description": model.description,
            "columns": _columns(model),
            "config": {
                "meta": model.meta,
                "materialized": "incremental" if incremental else "table",
                "incremental_strategy": "merge",
                "scd_type": model.scd_type,
            },
            "depends_on": {"nodes": depends},
            "original_file_path": model.path.relative_to(
                project.config.root).as_posix(),
            "raw_code": model.sql or "",
            "dbde": {"layer": model.layer, "kind": model.kind,
                     "key": model.key,
                     "business_keys": model.business_keys},
        }
        if model.measures:
            mv = f"mv_{manifest_name(model)[4:]}"
            body = {"source": manifest_name(model), "measures": [
                {"name": m.get("name"), "expr": m.get("expression"),
                 "comment": m.get("description")} for m in model.measures]}
            nodes[f"model.{name}.{mv}"] = {
                **nodes[uid], "unique_id": f"model.{name}.{mv}", "name": mv,
                "alias": model.metric_view,
                "relation_name": project.metric_view_relation(model,
                                                              catalogs),
                "columns": {}, "raw_code": yaml.safe_dump(body,
                                                          sort_keys=False),
                "config": {"meta": {"matrix": {"measures": {
                    str(m.get("name")): m.get("meta", {}).get("matrix", {})
                    for m in model.measures}}}},
                "depends_on": {"nodes": [uid]},
            }
    for test in dq.all_tests(project):
        node = _test_node(project, test)
        nodes[node["unique_id"]] = node
    if (project.config.root / GLOSSARY).exists():
        nodes[f"seed.{name}.business_glossary"] = {
            "unique_id": f"seed.{name}.business_glossary",
            "resource_type": "seed", "name": "business_glossary",
            "package_name": name, "original_file_path": GLOSSARY}
    return {
        "metadata": {
            "project_name": name, "generator": "dbde", "target": target,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "invocation_id": str(uuid.uuid4()),
        },
        "nodes": nodes, "sources": sources,
        "lineage": lineage(project, catalogs),
    }


def write(project: Project, target: str = "dev") -> str:
    """Write ``target/manifest.json``.

    Args:
        project: The loaded project.
        target: Target whose catalogs the relation names use.

    Returns:
        The path written (relative).
    """
    out = project.config.root / "target" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(project, target), indent=2, default=str),
                   encoding="utf-8")
    return "target/manifest.json"
