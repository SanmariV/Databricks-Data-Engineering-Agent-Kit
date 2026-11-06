"""Connections to a Databricks workspace: SQL warehouse and the bundle CLI.

Sign-in uses Databricks unified authentication (``databricks-sdk``). In CI
and production the service principal comes from ``DATABRICKS_HOST``,
``DATABRICKS_CLIENT_ID`` and ``DATABRICKS_CLIENT_SECRET``. On a laptop the
target's CLI profile in ``dbde.yml`` is used: create it once with
``databricks auth login --host <workspace url> --profile <profile>`` (OAuth;
no token is stored in the repo).

Each target in ``dbde.yml`` names its CLI profile, its SQL warehouse (by
name) and its catalogs. The repo never holds hosts, ids or secrets.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from dbde.project import Config

WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|"
    r"grant|revoke|deny|call|execute|exec|set|reset|use|declare|begin|"
    r"commit|rollback|copy|optimize|vacuum|refresh|msck|analyze|cache|"
    r"uncache|clone|restore|put|remove|load|into|overwrite|comment|"
    r"signal|undrop|sync|apply|fsck|reorg|zorder)\b", re.I)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
GO_LINE = re.compile(r"^\s*--\s*GO\s*$", re.M | re.I)


class TargetError(Exception):
    """Raised when a target is missing, incomplete or not allowed."""


def target_settings(config: Config, target: str, writes: bool) -> Dict:
    """Return the resolved settings of a target.

    Args:
        config: The project config.
        target: Target name, e.g. ``dev``.
        writes: True when the command changes the workspace.

    Returns:
        ``profile`` (empty in CI, where environment variables sign in),
        ``warehouse`` and ``catalogs``.

    Raises:
        TargetError: If the target is unknown, has no warehouse, or a write
            targets a protected target outside the release pipeline.
    """
    settings = config.targets.get(target)
    if settings is None:
        raise TargetError(f"target '{target}' is not in dbde.yml")
    if writes and settings.get("protected") and \
            os.environ.get("DBDE_ALLOW_PROTECTED") != "1":
        raise TargetError(
            f"target '{target}' is protected: only the release pipeline "
            "writes to it (it sets DBDE_ALLOW_PROTECTED=1). Use dev.")
    warehouse = str(settings.get("warehouse") or "")
    if not warehouse or warehouse.upper().startswith("TODO"):
        raise TargetError(f"set targets.{target}.warehouse in dbde.yml to the "
                          "name of a SQL warehouse")
    in_ci = bool(os.environ.get("DATABRICKS_HOST"))
    return {"profile": "" if in_ci else str(settings.get("profile") or ""),
            "warehouse": str(settings["warehouse"]),
            "catalogs": config.catalogs(target)}


def sdk_config(settings: Dict):
    """Return a ``databricks.sdk`` config for a target.

    Args:
        settings: Output of ``target_settings``.

    Returns:
        A resolved ``databricks.sdk.core.Config``.

    Raises:
        TargetError: If the SDK is missing or sign-in fails.
    """
    try:
        from databricks.sdk.core import Config as SdkConfig
    except ImportError as exc:
        raise TargetError("pip install databricks-sdk (requirements.txt)") \
            from exc
    try:
        if settings["profile"]:
            return SdkConfig(profile=settings["profile"])
        return SdkConfig()
    except Exception as exc:
        hint = (f"run: databricks auth login --host <workspace url> "
                f"--profile {settings['profile']}" if settings["profile"]
                else "set DATABRICKS_HOST and the service principal "
                     "variables, or a profile in dbde.yml")
        raise TargetError(f"Databricks sign-in failed ({exc}); {hint}") \
            from exc


def warehouse_id(config, name: str) -> str:
    """Return the id of a SQL warehouse by name.

    Args:
        config: A resolved SDK config.
        name: Warehouse name, as in ``dbde.yml``.

    Returns:
        The warehouse id.

    Raises:
        TargetError: If no warehouse has that name.
    """
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(config=config)
    names = []
    for warehouse in client.warehouses.list():
        names.append(warehouse.name)
        if warehouse.name == name:
            return str(warehouse.id)
    raise TargetError(f"SQL warehouse '{name}' not found; known: "
                      f"{', '.join(sorted(n for n in names if n)) or 'none'}")


def sql_connection(config: Config, target: str, writes: bool = False):
    """Open a connection to the target's SQL warehouse.

    Args:
        config: The project config.
        target: Target name.
        writes: True when the caller will change objects.

    Returns:
        An open ``databricks.sql`` connection.

    Raises:
        TargetError: If the connector is missing or sign-in fails.
    """
    settings = target_settings(config, target, writes)
    sdk = sdk_config(settings)
    try:
        from databricks import sql
    except ImportError as exc:
        raise TargetError("pip install databricks-sql-connector "
                          "(requirements.txt)") from exc
    host = sdk.host.removeprefix("https://").rstrip("/")
    path = f"/sql/1.0/warehouses/{warehouse_id(sdk, settings['warehouse'])}"
    return sql.connect(server_hostname=host, http_path=path,
                       credentials_provider=lambda: sdk.authenticate)


def query(connection, sql_text: str,
          limit: Optional[int] = None) -> List[Dict]:
    """Run a query and return the rows as dicts.

    Args:
        connection: Open connection.
        sql_text: One statement.
        limit: Maximum rows to fetch.

    Returns:
        Rows as column -> value.
    """
    with connection.cursor() as cursor:
        cursor.execute(sql_text)
        if cursor.description is None:
            return []
        names = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
    return [dict(zip(names, row)) for row in rows]


def _strip_literals(sql_text: str) -> str:
    """Blank out comments, strings and quoted names in one pass.

    A single left-to-right scan, so a quote cannot hide a comment
    marker and a comment cannot hide a quote (``select '/*'; delete
    ...; select '*/'`` is seen as two statements, not one).

    Args:
        sql_text: Query text.

    Returns:
        The text with every comment, string literal and ```name```
        identifier replaced by a space.

    Raises:
        TargetError: If a string, identifier or comment is not closed.
    """
    out, i, n = [], 0, len(sql_text)
    while i < n:
        two = sql_text[i:i + 2]
        if two == "--":
            end = sql_text.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif two == "/*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql_text[i:i + 2] == "/*":
                    depth, i = depth + 1, i + 2
                elif sql_text[i:i + 2] == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise TargetError("unclosed comment")
            out.append(" ")
        elif sql_text[i] in "'\"`":
            close, i = sql_text[i], i + 1
            while True:
                if i >= n:
                    raise TargetError("unclosed string or identifier")
                char = sql_text[i]
                if char == "\\" and close != "`":
                    i += 2
                    continue
                if char == close:
                    if sql_text[i + 1:i + 2] == close:   # doubled = escaped
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(sql_text[i])
            i += 1
    return "".join(out)


def assert_read_only(sql_text: str) -> None:
    """Refuse anything that is not a single read-only SELECT.

    This guards against mistakes, not against a hostile user: production
    reads use a principal with ``SELECT`` only.

    Args:
        sql_text: Query text.

    Raises:
        TargetError: If the text could change data or holds several
            statements.
    """
    if CONTROL_CHARS.search(sql_text):
        raise TargetError("control characters (including carriage returns) "
                          "are not allowed")
    text = _strip_literals(sql_text).strip().rstrip(";").strip()
    if ";" in text:
        raise TargetError("only one statement is allowed")
    if not re.match(r"^\s*(select|with)\b", text, re.I):
        raise TargetError("only SELECT (or WITH ... SELECT) is allowed")
    if WRITE_WORDS.search(text):
        raise TargetError("the query contains a write keyword; read-only only")


def read_only_query(connection, sql_text: str,
                    limit: Optional[int] = None) -> List[Dict]:
    """Run a checked SELECT and return the rows.

    Args:
        connection: Open connection.
        sql_text: One SELECT statement.
        limit: Maximum rows to fetch.

    Returns:
        Rows as column -> value.
    """
    assert_read_only(sql_text)
    return query(connection, sql_text, limit)


def statements(script: str) -> List[str]:
    """Split a rendered SQL file into its top-level statements.

    Rendered files end every top-level statement with a ``-- GO`` line, so
    procedures and ``BEGIN ... END`` blocks (which hold semicolons) stay
    whole.

    Args:
        script: Script text.

    Returns:
        Statements without their trailing semicolon; comment-only parts are
        dropped.
    """
    out = []
    for part in GO_LINE.split(script):
        if not re.sub(r"--[^\n]*", "", part).strip():
            continue
        out.append(part.strip().rstrip(";").rstrip())
    return out


def execute_script(connection, script: str) -> int:
    """Run every statement of a rendered SQL file.

    Args:
        connection: Open connection.
        script: Script text, catalog tokens already replaced.

    Returns:
        The number of statements run.
    """
    count = 0
    with connection.cursor() as cursor:
        for statement in statements(script):
            cursor.execute(statement)
            count += 1
    return count


def bundle(command: List[str], settings: Dict, target: str,
           root: str) -> None:
    """Run a ``databricks bundle`` command for a target.

    Args:
        command: Arguments after ``databricks bundle``, e.g. ``["deploy"]``.
        settings: Output of ``target_settings``.
        target: Target name (the bundle target of the same name).
        root: Project root (where ``databricks.yml`` lives).

    Raises:
        TargetError: If the CLI is missing or the command fails.
    """
    cli = shutil.which("databricks")
    if not cli:
        raise TargetError("install the Databricks CLI (1.3.0 or later): "
                          "winget install Databricks.DatabricksCLI")
    args = [cli, "bundle", *command, "--target", target]
    if settings["profile"]:
        args += ["--profile", settings["profile"]]
    env = dict(os.environ)
    env.setdefault("DATABRICKS_BUNDLE_ENGINE", "direct")
    print("$ " + " ".join(args[1:]))
    result = subprocess.run(args, cwd=root, env=env, check=False)
    if result.returncode:
        raise TargetError(f"databricks bundle {command[0]} failed "
                          f"(exit {result.returncode})")
