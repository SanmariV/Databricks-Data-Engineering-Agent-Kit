"""Read a column data dictionary from an .xlsx or .csv file, read-only.

Prints JSON: {"<column name lower-cased>": {"description": ..., "expanded": ...}}.
The name column is the header containing variable/column/field/name/attribute.
The description column contains description/definition/meaning; a header that
also contains "expanded"/"long"/"detail" is used as the expanded description.

Usage:
    python -I scripts/read_data_dictionary.py <path> [--sheet NAME]
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

NAME_KEYS = ("variable", "column", "field", "attribute", "name")
DESC_KEYS = ("description", "definition", "meaning")
LONG_KEYS = ("expanded", "long", "detail")


def clean(value: object) -> str:
    """Collapse runs of whitespace and strip; None becomes an empty string."""
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def read_rows(path: Path, sheet: str | None) -> list[list[object]]:
    """Return all rows of the first (or named) sheet, or of a CSV file."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.reader(handle))
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
    return [list(row) for row in worksheet.iter_rows(values_only=True)]


def find_column(headers: list[str], keys: tuple[str, ...]) -> list[int]:
    """Indexes of headers containing any of the keys, in column order."""
    return [i for i, h in enumerate(headers) if any(k in h.lower() for k in keys)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--sheet")
    args = parser.parse_args()

    rows = read_rows(args.path, args.sheet)
    if not rows:
        sys.exit("The file is empty.")
    headers = [clean(h) for h in rows[0]]
    names = find_column(headers, NAME_KEYS)
    descs = find_column(headers, DESC_KEYS)
    if not names or not descs:
        sys.exit(f"Could not find name and description columns in {headers}.")
    long_cols = [i for i in descs if any(k in headers[i].lower() for k in LONG_KEYS)]
    short_cols = [i for i in descs if i not in long_cols]
    result = {}
    for row in rows[1:]:
        name = clean(row[names[0]]) if len(row) > names[0] else ""
        if not name:
            continue
        get = lambda cols: clean(row[cols[0]]) if cols and len(row) > cols[0] else ""
        result[name.lower()] = {
            "column": name,
            "description": get(short_cols),
            "expanded": get(long_cols),
        }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
