"""Load the project config (``dbde.yml``) and the table specs (``models/``).

Layout read by this module::

    dbde.yml
    models/sources/<source>.yml                 bronze tables (read-only)
    models/silver/<schema>/<Table>.yml          silver source table (1:1)
    models/silver/<domain>/<Table>.yml + .sql   silver integrated table
    models/gold/<domain>/<Table>.yml + .sql     gold Dim/Fact/Bridge table
    models/genie/<Name>.yml                     Genie space definition

The schema of a model is its folder name and the table name is its file
name, so neither can drift from where the file lives.

Catalogs differ per environment (``dev_silver``, ``prod_silver``), so the
rendered code never names one. It uses the tokens ``${bronze_catalog}``,
``${silver_catalog}`` and ``${gold_catalog}``: notebooks replace them with
their job parameters, and ``python -m dbde deploy`` replaces them in the gold
SQL with the catalogs of the target in ``dbde.yml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

REF_PATTERN = re.compile(
    r"\{\{\s*ref\(\s*['\"](?P<key>[A-Za-z0-9_]+\.[A-Za-z0-9_]+)['\"]"
    r"\s*\)\s*\}\}")
SOURCE_PATTERN = re.compile(
    r"\{\{\s*source\(\s*['\"](?P<source>\w+)['\"]\s*,\s*"
    r"['\"](?P<table>\w+)['\"]\s*\)\s*\}\}")

LAYERS = ("bronze", "silver", "gold")
TOKENS = {layer: "${" + layer + "_catalog}" for layer in LAYERS}
AUDIT_COLUMNS = ["InsertLoadStepID", "InsertTimestamp",
                 "LastUpdateLoadStepID", "LastUpdateTimestamp"]
SCD_COLUMNS = ["StartDate", "EndDate", "IsCurrent"]
# Names the rendered stored procedures use for their parameters and local
# variables. A spec column with one of these names would be ambiguous inside
# the procedure, so the check step rejects it (rule S10).
PROCEDURE_NAMES = ["RunLoadDate", "RunLoadStepID", "RunTimestamp", "MaxKey",
                   "NewKey", "MergeKey1", "MergeKey2", "MergeKey3",
                   "MergeKey4", "TestLayer", "FailedTests", "FailureMessage"]
GOLD_PREFIXES = {"Dim": "dimension", "Fact": "fact", "Bridge": "bridge"}
METRIC_PREFIX = "Metric"


class SpecError(Exception):
    """Raised when a spec file cannot be read at all."""


@dataclass
class Config:
    """Settings from ``dbde.yml``.

    Attributes:
        root: Project root folder.
        project: Project name (snake_case); also the bundle name.
        targets: Target name -> settings (profile, warehouse, catalogs).
        raw: The whole parsed file.
    """

    root: Path
    project: str
    targets: Dict[str, Dict[str, Any]]
    raw: Dict[str, Any]

    def catalogs(self, target: str) -> Dict[str, str]:
        """Return the bronze, silver and gold catalogs of a target.

        Args:
            target: Target name, e.g. ``dev``.

        Returns:
            Layer -> catalog name. Missing entries default to
            ``<target>_<layer>``.
        """
        settings = self.targets.get(target) or {}
        given = settings.get("catalogs") or {}
        return {layer: str(given.get(layer) or f"{target}_{layer}")
                for layer in LAYERS}

    @property
    def layer_schemas(self) -> Dict[str, str]:
        """Return layer -> schema when each layer has one fixed schema.

        Workspaces with one catalog per project (``life_insurance.silver``,
        ``life_insurance.gold``) set ``layer_schemas`` in ``dbde.yml``. A
        layer that is not listed keeps one schema per spec folder.
        """
        given = self.raw.get("layer_schemas") or {}
        return {layer: str(given[layer]) for layer in LAYERS
                if given.get(layer)}

    @property
    def genie_pii(self) -> str:
        """Return how Genie items treat PII columns: ``exclude`` only."""
        return str((self.raw.get("genie") or {}).get("pii", "exclude"))

    @property
    def daily_load(self) -> Dict[str, Any]:
        """Return the daily job settings (schedule, timezone, emails)."""
        return dict(self.raw.get("daily_load") or {})

    @property
    def maintenance(self) -> Dict[str, Any]:
        """Return the table maintenance job settings."""
        return dict(self.raw.get("maintenance") or {})


@dataclass
class Column:
    """One column declared in a spec.

    Attributes:
        name: Column name.
        data_type: Spark / Databricks SQL type.
        description: Business meaning, unit and allowed values.
        business_key: True when the column is part of the logical key.
        expression: Spark SQL over bronze columns (silver source tables).
        references: ``schema.DimTable`` a gold ``_SK`` column points to.
        lineage: Optional explicit ``schema.Table.Column`` inputs.
        tests: Column tests as written in the spec.
        meta: Free metadata (``pii``, ``matrix``).
        synonyms: Other names the business uses (Genie and metric views).
    """

    name: str
    data_type: str
    description: Optional[str]
    business_key: bool = False
    expression: Optional[str] = None
    references: Optional[str] = None
    lineage: List[str] = field(default_factory=list)
    tests: List[Any] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    synonyms: Any = field(default_factory=list)

    @property
    def pii(self) -> bool:
        """Return True when the column is marked ``meta.pii: true``."""
        return self.meta.get("pii") is True


@dataclass
class Model:
    """One silver or gold table.

    Attributes:
        layer: ``silver`` or ``gold``.
        kind: ``source`` / ``integrated`` (silver), ``dimension`` / ``fact`` /
            ``bridge`` (gold).
        schema: Schema name (the folder). Used by ``ref()`` and file paths.
        name: Table name (the file name).
        path: Spec file.
        sql: Body of the ``.sql`` file, or None.
        spec: The parsed spec.
        columns: Declared columns, in order.
        db_schema: Schema the table is created in: the folder, or the
            layer's schema when ``dbde.yml`` sets ``layer_schemas``.
    """

    layer: str
    kind: str
    schema: str
    name: str
    path: Path
    sql: Optional[str]
    spec: Dict[str, Any]
    columns: List[Column]
    db_schema: str = ""

    @property
    def key(self) -> str:
        """Return ``schema.Table``, the name used by ``ref()``."""
        return f"{self.schema}.{self.name}"

    @property
    def table(self) -> str:
        """Return ``schema.Table`` as created in the catalog."""
        return f"{self.db_schema or self.schema}.{self.name}"

    @property
    def description(self) -> str:
        """Return the model description on one line."""
        return " ".join(str(self.spec.get("description") or "").split())

    @property
    def business_keys(self) -> List[str]:
        """Return the business key column names, in declared order."""
        return [c.name for c in self.columns if c.business_key]

    @property
    def data_columns(self) -> List[Column]:
        """Return the columns that are not business keys."""
        return [c for c in self.columns if not c.business_key]

    @property
    def refs(self) -> List[str]:
        """Return the ``schema.Table`` keys referenced in the ``.sql`` file."""
        if not self.sql:
            return []
        return list(dict.fromkeys(m.group("key")
                                  for m in REF_PATTERN.finditer(self.sql)))

    @property
    def source(self) -> Optional[Tuple[str, str]]:
        """Return (source, table) for a silver source table, else None."""
        src = self.spec.get("source")
        if isinstance(src, dict) and src.get("source") and src.get("table"):
            return str(src["source"]), str(src["table"])
        return None

    @property
    def load(self) -> Dict[str, Any]:
        """Return the load settings (``mode``, column, lookback)."""
        return dict(self.spec.get("load") or {"mode": "full"})

    @property
    def scd_type(self) -> Optional[int]:
        """Return the SCD type of a dimension (1 or 2)."""
        value = self.spec.get("scd_type")
        return int(value) if value not in (None, "") else None

    @property
    def surrogate_key(self) -> str:
        """Return the ``{Table}_SK`` column name (gold only)."""
        return f"{self.name}_SK"

    @property
    def is_scd2(self) -> bool:
        """Return True for a type 2 dimension."""
        return self.kind == "dimension" and self.scd_type == 2

    @property
    def tests(self) -> List[Any]:
        """Return the table-level tests."""
        return list(self.spec.get("tests") or [])

    @property
    def meta(self) -> Dict[str, Any]:
        """Return the table metadata."""
        return dict(self.spec.get("meta") or {})

    @property
    def measures(self) -> List[Dict[str, Any]]:
        """Return the declared measures (facts only)."""
        return [m for m in self.spec.get("measures") or []
                if isinstance(m, dict)]

    @property
    def metric_view(self) -> Optional[str]:
        """Return the metric view name of a fact with measures, else None.

        ``FactNewBusiness`` -> ``MetricNewBusiness``.
        """
        if self.kind != "fact" or not self.measures:
            return None
        return METRIC_PREFIX + self.name[len("Fact"):]

    @property
    def synonyms(self) -> Any:
        """Return the table synonyms (Genie and metric views)."""
        return self.spec.get("synonyms") or []

    @property
    def ai_instructions(self) -> str:
        """Return the table guidance written for Genie."""
        return " ".join(str(self.spec.get("ai_instructions") or "").split())


@dataclass
class SourceTable:
    """One bronze table.

    Attributes:
        source: Source system name.
        name: Table name in bronze.
        description: What the table holds.
        columns: Column name -> data type as Unity Catalog reports it.
        meta: Free metadata.
    """

    source: str
    name: str
    description: Optional[str]
    columns: Dict[str, str]
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Source:
    """One bronze source system (a schema in the bronze catalog).

    Attributes:
        name: Source system name.
        schema: Schema in the bronze catalog.
        description: What the source is and how it lands.
        freshness: ``loaded_at_column``, ``warn_after_hours``,
            ``error_after_hours``.
        tables: Table name -> table.
        path: Spec file.
    """

    name: str
    schema: str
    description: Optional[str]
    freshness: Dict[str, Any]
    tables: Dict[str, SourceTable]
    path: Path


@dataclass
class GenieSpace:
    """One Genie space, from ``models/genie/<Name>.yml``.

    Attributes:
        name: Space name (the file name, PascalCase).
        path: Spec file.
        spec: The parsed spec.
    """

    name: str
    path: Path
    spec: Dict[str, Any]

    @property
    def resource_name(self) -> str:
        """Return the bundle resource key, ``genie_<snake_name>``."""
        return f"genie_{snake(self.name)}"

    @property
    def title(self) -> str:
        """Return the title shown in Databricks."""
        return " ".join(str(self.spec.get("title") or "").split())

    @property
    def description(self) -> str:
        """Return the space description."""
        return " ".join(str(self.spec.get("description") or "").split())

    @property
    def instructions(self) -> str:
        """Return the general instructions Genie follows."""
        return str(self.spec.get("instructions") or "").strip()

    @property
    def tables(self) -> List[str]:
        """Return the ``schema.Table`` keys of the tables and metric views."""
        return [str(t) for t in self.spec.get("tables") or []]

    @property
    def sample_questions(self) -> List[str]:
        """Return the sample questions shown to users."""
        return [str(q) for q in self.spec.get("sample_questions") or []]

    @property
    def examples(self) -> List[Dict[str, Any]]:
        """Return the example question / SQL pairs."""
        return [e for e in self.spec.get("examples") or []
                if isinstance(e, dict)]

    @property
    def benchmarks(self) -> List[Dict[str, Any]]:
        """Return the benchmark question / SQL pairs."""
        return [e for e in self.spec.get("benchmarks") or []
                if isinstance(e, dict)]

    @property
    def groups(self) -> List[str]:
        """Return the consumer groups granted CAN_RUN on the space."""
        return [str(g) for g in
                (self.spec.get("permissions") or {}).get("can_run") or []]


@dataclass
class Project:
    """Everything under ``models/`` plus the config.

    Attributes:
        config: The parsed ``dbde.yml``.
        sources: Source name -> source.
        models: ``schema.Table`` -> model.
        errors: Problems found while loading (file, message).
        spaces: Space name -> Genie space definition.
    """

    config: Config
    sources: Dict[str, Source]
    models: Dict[str, Model]
    errors: List[Tuple[str, str]]
    spaces: Dict[str, GenieSpace] = field(default_factory=dict)

    def silver(self) -> List[Model]:
        """Return the silver models, sorted by key."""
        return [m for _, m in sorted(self.models.items())
                if m.layer == "silver"]

    def gold(self) -> List[Model]:
        """Return the gold models, sorted by key."""
        return [m for _, m in sorted(self.models.items()) if m.layer == "gold"]

    @property
    def metric_views(self) -> Dict[str, Model]:
        """Return ``schema.MetricX`` -> the fact its metric view reads."""
        return {f"{m.schema}.{m.metric_view}": m for m in self.gold()
                if m.metric_view}

    def relation(self, model: Model,
                 catalogs: Optional[Dict[str, str]] = None) -> str:
        """Return the three-part name a model is queried by.

        Args:
            model: The model.
            catalogs: Layer -> catalog. None renders the catalog tokens.

        Returns:
            ``catalog.schema.Table``.
        """
        catalog = (catalogs or TOKENS)[model.layer]
        return f"{catalog}.{model.table}"

    def metric_view_relation(self, fact: Model,
                             catalogs: Optional[Dict[str, str]] = None) -> str:
        """Return the three-part name of a fact's metric view.

        Args:
            fact: A fact with measures.
            catalogs: Layer -> catalog. None renders the catalog tokens.

        Returns:
            ``catalog.schema.MetricX``.
        """
        catalog = (catalogs or TOKENS)["gold"]
        return f"{catalog}.{fact.db_schema or fact.schema}."             f"{fact.metric_view}"

    def source_relation(self, source: str, table: str,
                        catalogs: Optional[Dict[str, str]] = None) -> str:
        """Return the three-part name of a bronze table.

        Args:
            source: Source system name.
            table: Bronze table name.
            catalogs: Layer -> catalog. None renders the catalog tokens.

        Returns:
            ``catalog.schema.table``.
        """
        schema = self.sources[source].schema if source in self.sources \
            else source
        return f"{(catalogs or TOKENS)['bronze']}.{schema}.{table}"

    def resolve_sql(self, sql: str,
                    catalogs: Optional[Dict[str, str]] = None) -> str:
        """Replace ``{{ ref() }}`` and ``{{ source() }}`` with real names.

        Args:
            sql: SQL text with placeholders.
            catalogs: Layer -> catalog. None renders the catalog tokens,
                which notebooks and ``dbde deploy`` replace later.

        Returns:
            The SQL with every known placeholder replaced. Unknown refs are
            left in place, so the check step can report them.
        """
        views = self.metric_views

        def ref(match: re.Match) -> str:
            key = match.group("key")
            if key in self.models:
                return self.relation(self.models[key], catalogs)
            if key in views:
                return self.metric_view_relation(views[key], catalogs)
            return match.group(0)

        def src(match: re.Match) -> str:
            return self.source_relation(match.group("source"),
                                        match.group("table"), catalogs)

        return SOURCE_PATTERN.sub(src, REF_PATTERN.sub(ref, sql))

    def load_order(self, models: List[Model]) -> List[Model]:
        """Sort models so every model comes after the models it reads.

        Args:
            models: Models of one layer.

        Returns:
            The same models in dependency order (cycles keep file order).
        """
        keys = {m.key for m in models}
        done: List[Model] = []
        seen: set = set()

        def visit(model: Model, stack: Tuple[str, ...]) -> None:
            if model.key in seen or model.key in stack:
                return
            for ref in model.refs:
                if ref in keys and ref != model.key:
                    visit(self.models[ref], stack + (model.key,))
            seen.add(model.key)
            done.append(model)

        for model in sorted(models, key=lambda m: (m.kind != "dimension",
                                                   m.key)):
            visit(model, ())
        return done


def substitute(text: str, catalogs: Dict[str, str]) -> str:
    """Replace the catalog tokens in rendered SQL with real catalog names.

    Args:
        text: SQL holding ``${bronze_catalog}`` style tokens.
        catalogs: Layer -> catalog name.

    Returns:
        The SQL with every token replaced.
    """
    for layer, token in TOKENS.items():
        text = text.replace(token, catalogs[layer])
    return text


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Read one YAML file into a dict.

    Args:
        path: File to read.

    Returns:
        The parsed mapping (empty when the file is empty).

    Raises:
        SpecError: If the file is not valid YAML or not a mapping.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("expected a mapping at the top of the file")
    return data


def load_config(root: Path) -> Config:
    """Read ``dbde.yml`` from the project root.

    Args:
        root: Project root folder.

    Returns:
        The config.

    Raises:
        SpecError: If ``dbde.yml`` is missing.
    """
    path = root / "dbde.yml"
    if not path.exists():
        raise SpecError(f"{path} not found; run dbde from the project root")
    raw = _read_yaml(path)
    return Config(root=root,
                  project=str(raw.get("project") or "databricks_project"),
                  targets=dict(raw.get("targets") or {}), raw=raw)


def _column(raw: Any) -> Column:
    """Build a column from its spec entry.

    Args:
        raw: The YAML mapping for one column.

    Returns:
        The column.
    """
    raw = raw if isinstance(raw, dict) else {"name": str(raw)}
    lineage = raw.get("lineage") or []
    return Column(
        name=str(raw.get("name") or ""),
        data_type=str(raw.get("data_type") or "").strip().lower(),
        description=raw.get("description"),
        business_key=bool(raw.get("business_key", False)),
        expression=raw.get("expression"),
        references=raw.get("references"),
        lineage=[lineage] if isinstance(lineage, str) else list(lineage),
        tests=list(raw.get("tests") or []),
        meta=dict(raw.get("meta") or {}),
        synonyms=raw.get("synonyms") or [],
    )


def _load_source(path: Path, default_schema: str = "") -> Source:
    """Read one ``models/sources/<source>.yml`` file.

    Args:
        path: Spec file.
        default_schema: Bronze schema when the spec names none; empty means
            the source name.

    Returns:
        The source.
    """
    raw = _read_yaml(path)
    name = str(raw.get("source") or path.stem)
    tables = {}
    for table in raw.get("tables") or []:
        cols = {}
        for col in table.get("columns") or []:
            if isinstance(col, dict):
                cols[str(col.get("name"))] = str(col.get("data_type") or "")
            else:
                cols[str(col)] = ""
        tables[str(table["name"])] = SourceTable(
            source=name, name=str(table["name"]),
            description=table.get("description"), columns=cols,
            meta=dict(table.get("meta") or {}))
    return Source(name=name,
                  schema=str(raw.get("schema") or default_schema or name),
                  description=raw.get("description"),
                  freshness=dict(raw.get("freshness") or {}),
                  tables=tables, path=path)


def _gold_kind(name: str) -> str:
    """Return the gold kind from the table name prefix.

    Args:
        name: Gold table name.

    Returns:
        ``dimension``, ``fact``, ``bridge`` or ``unknown``.
    """
    for prefix, kind in GOLD_PREFIXES.items():
        if name.startswith(prefix) and name[len(prefix):][:1].isupper():
            return kind
    return "unknown"


def _load_model(path: Path, layer: str) -> Model:
    """Read one silver or gold spec plus its ``.sql`` file.

    Args:
        path: Spec file.
        layer: ``silver`` or ``gold``.

    Returns:
        The model.
    """
    raw = _read_yaml(path)
    sql_path = path.with_suffix(".sql")
    sql = sql_path.read_text(encoding="utf-8") if sql_path.exists() else None
    if layer == "silver":
        kind = "source" if raw.get("source") else "integrated"
    else:
        kind = _gold_kind(path.stem)
    return Model(layer=layer, kind=kind, schema=path.parent.name,
                 name=path.stem, path=path, sql=sql, spec=raw,
                 columns=[_column(c) for c in raw.get("columns") or []])


def load_project(root: Path) -> Project:
    """Load the config and every spec under ``models/``.

    Args:
        root: Project root folder.

    Returns:
        The project. Files that could not be read are listed in ``errors``.
    """
    config = load_config(root)
    errors: List[Tuple[str, str]] = []
    sources: Dict[str, Source] = {}
    models: Dict[str, Model] = {}
    base = root / "models"
    for path in sorted((base / "sources").glob("*.yml")):
        try:
            source = _load_source(path,
                                  config.layer_schemas.get("bronze", ""))
            sources[source.name] = source
        except (SpecError, KeyError, TypeError, AttributeError) as exc:
            errors.append((rel(path, root), str(exc)))
    for layer in ("silver", "gold"):
        for path in sorted((base / layer).glob("*/*.yml")):
            try:
                model = _load_model(path, layer)
            except SpecError as exc:
                errors.append((rel(path, root), str(exc)))
                continue
            model.db_schema = config.layer_schemas.get(layer, model.schema)
            if model.key in models:
                errors.append((rel(path, root),
                               f"{model.key} is defined twice"))
            models[model.key] = model
        for path in sorted((base / layer).glob("*/*.sql")):
            if not path.with_suffix(".yml").exists():
                errors.append((rel(path, root),
                               "SQL file has no matching .yml spec"))
    spaces: Dict[str, GenieSpace] = {}
    for path in sorted((base / "genie").glob("*.yml")):
        try:
            spaces[path.stem] = GenieSpace(name=path.stem, path=path,
                                           spec=_read_yaml(path))
        except SpecError as exc:
            errors.append((rel(path, root), str(exc)))
    return Project(config=config, sources=sources, models=models,
                   errors=errors, spaces=spaces)


def rel(path: Path, root: Path) -> str:
    """Return a path relative to the project root, with forward slashes.

    Args:
        path: Any path inside the project.
        root: Project root folder.

    Returns:
        The relative path as text.
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def snake(name: str) -> str:
    """Convert PascalCase to snake_case.

    Args:
        name: A PascalCase name such as ``NewBusiness``.

    Returns:
        The snake_case form, e.g. ``new_business``.
    """
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return text.lower()


def identifiers(expression: str) -> List[str]:
    """Return the bare identifiers in a SQL expression (lower case).

    Args:
        expression: A Spark SQL expression.

    Returns:
        Identifier tokens outside string literals, lower-cased.
    """
    text = re.sub(r"'[^']*'", "", expression or "")
    return [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)]


def sql_string(text: str) -> str:
    """Return a Spark SQL string literal.

    Args:
        text: Any text.

    Returns:
        The text in single quotes, with quotes and backslashes escaped.
    """
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"
