"""Shared silver-layer functions used by every rendered silver notebook.

DE Policy v2, silver section: explicit data types, a logical primary key,
Delta MERGE (never truncate-and-reload) and the four audit columns on
every table. These functions implement those rules once, so the notebooks
only carry table-specific settings.

The module never reads widgets or ``dbutils``: notebooks pass every value in,
so the functions can be tested without a cluster.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Sequence, Tuple

AUDIT_COLUMNS = [
    ("InsertLoadStepID", "string", "Load step that first wrote the row."),
    ("InsertTimestamp", "timestamp", "When the row was first written (UTC)."),
    ("LastUpdateLoadStepID", "string", "Load step that last changed the "
     "row."),
    ("LastUpdateTimestamp", "timestamp", "When the row was last changed "
     "(UTC)."),
]
ORDER_PREFIX = "__order_"
TOKENS = {"bronze": "${bronze_catalog}", "silver": "${silver_catalog}",
          "gold": "${gold_catalog}"}

# (name, Spark type, expression, comment)
Column = Tuple[str, str, str, str]


def resolve_load_date(load_date: str, today: Optional[dt.date] = None) -> str:
    """Return the load date, defaulting to yesterday (UTC).

    DE Policy v2: loads receive the previous day's date, so only that day's
    data moves.

    Args:
        load_date: ``yyyy-mm-dd`` or an empty string.
        today: Override for "today" (tests only).

    Returns:
        A ``yyyy-mm-dd`` date.

    Raises:
        ValueError: If ``load_date`` is not a valid date.
    """
    if load_date and load_date.strip():
        return dt.date.fromisoformat(load_date.strip()).isoformat()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return (today - dt.timedelta(days=1)).isoformat()


def substitute(text: str, catalogs: Dict[str, str]) -> str:
    """Replace the catalog tokens in rendered SQL with the job's catalogs.

    Args:
        text: SQL holding ``${silver_catalog}`` style tokens.
        catalogs: Layer -> catalog name.

    Returns:
        The SQL with every token replaced.

    Raises:
        ValueError: If a token is used but its catalog is empty.
    """
    for layer, token in TOKENS.items():
        if token in text:
            if not catalogs.get(layer):
                raise ValueError(f"the {layer}_catalog parameter is empty")
            text = text.replace(token, catalogs[layer])
    return text


def _quote_comment(text: str) -> str:
    """Return a Spark SQL string literal for a comment."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def select_expressions(columns: Sequence[Column],
                       order_by: Sequence[str]) -> List[str]:
    """Return the Spark SQL select list that renames and casts columns.

    Args:
        columns: (name, Spark type, expression, comment) per silver column.
        order_by: Dedupe ordering expressions over the input columns,
            e.g. ``_ingested_at desc``. Each becomes a hidden column.

    Returns:
        Expressions for ``DataFrame.selectExpr``.
    """
    exprs = [f"cast(({expr}) as {data_type}) as `{name}`"
             for name, data_type, expr, _ in columns]
    for i, item in enumerate(order_by):
        direction = "desc" if _is_descending(item) else "asc"
        exprs.append(f"{_order_expression(item)} as "
                     f"`{ORDER_PREFIX}{i}_{direction}`")
    return exprs


def _order_expression(item: str) -> str:
    """Strip ``asc``/``desc`` from an ordering item.

    Args:
        item: e.g. ``_ingested_at desc``.

    Returns:
        The expression without its direction.
    """
    words = item.strip().split()
    if len(words) > 1 and words[-1].lower() in ("asc", "desc"):
        return " ".join(words[:-1])
    return item.strip()


def _is_descending(item: str) -> bool:
    """Return True when an ordering item ends with ``desc``.

    Args:
        item: e.g. ``_ingested_at desc``.

    Returns:
        Whether to sort descending.
    """
    return item.strip().lower().endswith(" desc")


def merge_condition(keys: Sequence[str]) -> str:
    """Return the MERGE join condition on the business keys.

    Args:
        keys: Business key columns.

    Returns:
        e.g. ``t.`Customer_BK` = s.`Customer_BK```.
    """
    return " and ".join(f"t.`{k}` = s.`{k}`" for k in keys)


def change_condition(columns: Sequence[str]) -> Optional[str]:
    """Return a null-safe "any data column changed" condition.

    Args:
        columns: Data (non-key) columns.

    Returns:
        The condition, or None when there are no data columns.
    """
    if not columns:
        return None
    return " or ".join(f"not (t.`{c}` <=> s.`{c}`)" for c in columns)


def table_ddl(table: str, columns: Sequence[Column], keys: Sequence[str],
              description: str) -> str:
    """Return ``CREATE TABLE IF NOT EXISTS`` for a silver table.

    Column order follows the policy: business keys, data, audit columns.
    The business key is declared as the table's (informational) primary
    key in Unity Catalog: DE Policy v2, every table has a logical primary
    key.

    Args:
        table: ``catalog.schema.Table``.
        columns: (name, Spark type, expression, comment) per silver column.
        keys: Business key columns (created NOT NULL).
        description: Table comment.

    Returns:
        Spark SQL DDL.
    """
    ordered = ([c for c in columns if c[0] in keys]
               + [c for c in columns if c[0] not in keys])
    lines = [f"`{name}` {data_type}{' NOT NULL' if name in keys else ''}"
             f" COMMENT {_quote_comment(comment)}"
             for name, data_type, _, comment in ordered]
    lines += [f"`{name}` {data_type} NOT NULL COMMENT {_quote_comment(text)}"
              for name, data_type, text in AUDIT_COLUMNS]
    if keys:
        name = table.rsplit(".", 1)[-1]
        lines.append(f"CONSTRAINT `{name}_pk` PRIMARY KEY ("
                     + ", ".join(f"`{k}`" for k in keys) + ")")
    return (f"CREATE TABLE IF NOT EXISTS {table} (\n  "
            + ",\n  ".join(lines)
            + f"\n) USING DELTA COMMENT {_quote_comment(description)}")


def read_bronze(spark, table: str, expected_columns: Sequence[str],
                watermark_column: Optional[str], load_date: str,
                lookback_days: int):
    """Read a bronze table, checking the columns this notebook relies on.

    Args:
        spark: The Spark session.
        table: ``catalog.schema.table`` in bronze.
        expected_columns: Columns that must exist (case-insensitive).
        watermark_column: Load timestamp column for incremental reads, or
            None to read the whole table.
        load_date: Business date (``yyyy-mm-dd``).
        lookback_days: Days before ``load_date`` to read again, so late
            rows are not missed.

    Returns:
        The bronze rows as a DataFrame.

    Raises:
        ValueError: If an expected column is missing from bronze.
    """
    frame = spark.read.table(table)
    present = {c.lower() for c in frame.columns}
    missing = [c for c in expected_columns if c.lower() not in present]
    if missing:
        raise ValueError(f"{table} is missing columns {missing}; the source "
                         "changed. Update the source and silver specs.")
    if watermark_column:
        start = (dt.date.fromisoformat(load_date)
                 - dt.timedelta(days=lookback_days)).isoformat()
        frame = frame.where(f"`{watermark_column}` >= date'{start}'")
    return frame


def conform(frame, columns: Sequence[Column], order_by: Sequence[str]):
    """Rename and cast every column to its declared type.

    Args:
        frame: Input rows.
        columns: (name, Spark type, expression, comment) per silver column.
        order_by: Dedupe ordering, kept as hidden columns.

    Returns:
        The conformed DataFrame.
    """
    return frame.selectExpr(*select_expressions(columns, order_by))


def latest_per_key(frame, keys: Sequence[str]):
    """Keep one row per business key: the first by the dedupe ordering.

    Args:
        frame: Output of ``conform`` with hidden ordering columns.
        keys: Business key columns.

    Returns:
        One row per key, without the hidden columns.

    Raises:
        ValueError: If a key repeats and no ordering was given.
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    hidden = [c for c in frame.columns if c.startswith(ORDER_PREFIX)]
    if not hidden:
        assert_unique(frame, keys)
        return frame
    order = []
    for name in sorted(hidden,
                       key=lambda c: int(c[len(ORDER_PREFIX):].split("_")[0])):
        column = F.col(name)
        order.append(column.desc_nulls_last() if name.endswith("_desc")
                     else column.asc_nulls_last())
    window = Window.partitionBy(*keys).orderBy(*order)
    ranked = frame.withColumn("__row", F.row_number().over(window))
    return ranked.where("__row = 1").drop("__row", *hidden)


def assert_unique(frame, keys: Sequence[str]) -> None:
    """Fail when a business key appears more than once.

    Args:
        frame: Rows about to be merged.
        keys: Business key columns.

    Raises:
        ValueError: If any key repeats (the MERGE would be ambiguous).
    """
    duplicates = frame.groupBy(*keys).count().where("count > 1").limit(1)
    if duplicates.count():
        raise ValueError(f"business key {list(keys)} is not unique; fix the "
                         "query or add dedupe_order_by to the spec")


def ensure_table(spark, table: str, columns: Sequence[Column],
                 keys: Sequence[str], description: str) -> None:
    """Create the silver table, add new spec columns and sync comments.

    Schema changes are explicit: new columns are added with ALTER TABLE,
    never by schema inference or ``mergeSchema``. Column comments follow
    the spec, so Unity Catalog (and Genie) always see the current
    descriptions.

    Args:
        spark: The Spark session.
        table: ``catalog.schema.Table``.
        columns: (name, Spark type, expression, comment) per silver column.
        keys: Business key columns.
        description: Table comment.
    """
    catalog_schema = table.rsplit(".", 1)[0]
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog_schema}")
    spark.sql(table_ddl(table, columns, keys, description))
    fields = {f.name.lower(): f for f in spark.read.table(table).schema}
    new = [f"`{name}` {data_type} COMMENT {_quote_comment(comment)}"
           for name, data_type, _, comment in columns
           if name.lower() not in fields]
    if new:
        spark.sql(f"ALTER TABLE {table} ADD COLUMNS ({', '.join(new)})")
    for name, _, _, comment in columns:
        current = fields.get(name.lower())
        if current is not None and \
                current.metadata.get("comment", "") != comment:
            spark.sql(f"ALTER TABLE {table} ALTER COLUMN `{name}` "
                      f"COMMENT {_quote_comment(comment)}")
    if (spark.catalog.getTable(table).description or "") != description:
        spark.sql(f"COMMENT ON TABLE {table} IS "
                  f"{_quote_comment(description)}")


def merge_into(spark, table: str, frame, keys: Sequence[str],
               load_step_id: str) -> int:
    """MERGE rows into a silver table on its business keys.

    Insert* audit columns are set on insert only; LastUpdate* columns on
    insert and on every update. Unchanged rows are not touched.

    Args:
        spark: The Spark session.
        table: ``catalog.schema.Table``.
        frame: One row per business key, with the silver columns.
        keys: Business key columns.
        load_step_id: Load step identifier for the audit columns.

    Returns:
        The number of rows in ``frame``.
    """
    from delta.tables import DeltaTable
    from pyspark.sql import functions as F

    data = [c for c in frame.columns if c not in keys]
    now = F.current_timestamp()
    step = F.lit(load_step_id)
    target = DeltaTable.forName(spark, table)
    merge = target.alias("t").merge(frame.alias("s"), merge_condition(keys))
    changed = change_condition(data)
    if changed:
        merge = merge.whenMatchedUpdate(condition=changed, set={
            **{f"`{c}`": F.col(f"s.`{c}`") for c in data},
            "LastUpdateLoadStepID": step,
            "LastUpdateTimestamp": now,
        })
    merge.whenNotMatchedInsert(values={
        **{f"`{c}`": F.col(f"s.`{c}`") for c in frame.columns},
        "InsertLoadStepID": step,
        "InsertTimestamp": now,
        "LastUpdateLoadStepID": step,
        "LastUpdateTimestamp": now,
    }).execute()
    return frame.count()
