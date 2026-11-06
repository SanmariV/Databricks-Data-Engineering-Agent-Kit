"""Operational helpers for the ops notebooks: gold loads, tests, maintenance.

The gold logic lives in Unity Catalog stored procedures. These functions
only call them with checked names and bound parameters, so a notebook
parameter can never inject SQL.
"""

from __future__ import annotations

import re
from typing import Sequence

from databricks_de_common.silver import resolve_load_date

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LAYERS = ("source", "silver", "gold")

__all__ = ["call_load_procedure", "maintain_tables", "resolve_load_date",
           "run_data_quality_tests"]


def _qualified(catalog: str, name: str) -> str:
    """Return ``catalog.schema.object`` after checking every part.

    Args:
        catalog: Catalog name.
        name: ``schema.object``.

    Returns:
        The three-part name, each part backtick-quoted.

    Raises:
        ValueError: If a part is empty or not a plain identifier.
    """
    parts = [catalog] + name.split(".")
    if len(parts) != 3 or not all(NAME.match(p or "") for p in parts):
        raise ValueError(f"'{catalog}.{name}' is not catalog.schema.object")
    return ".".join(f"`{p}`" for p in parts)


def call_load_procedure(spark, gold_catalog: str, procedure: str,
                        load_date: str, load_step_id: str) -> None:
    """Call one gold ``Load<Table>`` stored procedure.

    Args:
        spark: The Spark session.
        gold_catalog: Gold catalog of the target.
        procedure: ``schema.Load<Table>``.
        load_date: Business date (``yyyy-mm-dd``).
        load_step_id: Load step identifier for the audit columns.
    """
    name = _qualified(gold_catalog, procedure)
    spark.sql(f"CALL {name}(CAST(:load_date AS DATE), :load_step_id)",
              args={"load_date": load_date, "load_step_id": load_step_id})


def run_data_quality_tests(spark, gold_catalog: str, layer: str,
                           load_step_id: str) -> None:
    """Run the data quality tests of one layer.

    ``ops.RunDataQualityTests`` raises an error when an error-severity test
    fails, which fails the task and stops the job.

    Args:
        spark: The Spark session.
        gold_catalog: Gold catalog of the target.
        layer: ``source``, ``silver`` or ``gold``.
        load_step_id: Load step identifier the results are logged under.

    Raises:
        ValueError: If the layer is unknown.
    """
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {LAYERS}, not '{layer}'")
    name = _qualified(gold_catalog, "ops.RunDataQualityTests")
    spark.sql(f"CALL {name}(:layer, :load_step_id)",
              args={"layer": layer, "load_step_id": load_step_id})


def maintain_tables(spark, tables: Sequence[str],
                    retain_hours: int = 168) -> None:
    """Run OPTIMIZE and VACUUM on every table that exists.

    DE Policy v2: Delta tables are actively maintained, with scheduled
    Vacuum and Optimise in place before production.

    Args:
        spark: The Spark session.
        tables: ``catalog.schema.Table`` names.
        retain_hours: VACUUM retention; never below 168 (7 days), so time
            travel and running readers keep working.

    Raises:
        ValueError: If ``retain_hours`` is below 168.
    """
    if retain_hours < 168:
        raise ValueError("retain at least 168 hours (7 days)")
    for table in tables:
        catalog, name = table.split(".", 1)
        qualified = _qualified(catalog, name)
        if not spark.catalog.tableExists(table):
            print(f"skip {table}: not built yet")
            continue
        spark.sql(f"OPTIMIZE {qualified}")
        spark.sql(f"VACUUM {qualified} RETAIN {int(retain_hours)} HOURS")
        print(f"maintained {table}")
