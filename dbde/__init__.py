"""dbde: the Databricks data engineering toolkit behind this kit.

It does for a Databricks Unity Catalog project what dbt does for a dbt
project: it reads table specs (``models/``), checks them, renders the code
Databricks runs (silver notebooks, gold tables, stored procedures, metric
views, Genie spaces, the Asset Bundle and its jobs) and runs builds and tests
against a workspace.

Run ``python -m dbde --help`` for the commands.
"""

__version__ = "0.1.0"
