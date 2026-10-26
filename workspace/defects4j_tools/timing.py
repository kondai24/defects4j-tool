"""Record per-bug elapsed time of the prompts / fix steps in <target_dir>/results.csv.

Columns:
- prompt_time_sec: time to build one prompt (step 2)
- fix_time_sec: time of one fix session (Claude / Codex, step 3)
- prompt_fix_time_sec: prompt_time_sec + fix_time_sec (empty unless both are set)
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional

from .fix_common import FixResponse

PROMPT_TIME_COL = "prompt_time_sec"
FIX_TIME_COL = "fix_time_sec"
TOTAL_TIME_COL = "prompt_fix_time_sec"
TIME_COLUMNS = [PROMPT_TIME_COL, FIX_TIME_COL, TOTAL_TIME_COL]
FIX_USAGE_COLUMNS = ["total_cost_usd", "num_turns", "input_tokens", "output_tokens"]


def format_seconds(seconds: Optional[float]) -> str:
    return "" if seconds is None else f"{seconds:.2f}"


def _total(row: Dict[str, str]) -> str:
    try:
        return format_seconds(float(row[PROMPT_TIME_COL]) + float(row[FIX_TIME_COL]))
    except (KeyError, TypeError, ValueError):
        return ""


def read_column(results_csv: Path, column: str) -> Dict[tuple, str]:
    """Return {(project, id): value} for one column (missing column -> empty dict)."""
    if not results_csv.exists():
        return {}
    with results_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or column not in reader.fieldnames:
            return {}
        return {
            ((row.get("project") or "").strip(), (row.get("id") or "").strip()): (row.get(column) or "").strip()
            for row in reader
        }


def _rewrite(results_csv: Path, update, extra_columns=()) -> None:
    """Apply update(row) to every row and rewrite results.csv, keeping all other columns."""
    with results_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames: List[str] = list(reader.fieldnames or [])
        rows = list(reader)

    for col in [*TIME_COLUMNS, *extra_columns]:
        if col not in fieldnames:
            fieldnames.append(col)

    for row in rows:
        update(row)

    with results_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def clear_column(results_csv: Path, column: str) -> None:
    """Empty one time column for all rows (so a re-run does not leave stale values)."""

    def update(row: Dict[str, str]) -> None:
        row[column] = ""
        row[TOTAL_TIME_COL] = _total(row)

    _rewrite(results_csv, update)


def clear_fix_usage(results_csv: Path) -> None:
    """Clear all usage from the previous fix run, including skipped rows."""
    def update(row: Dict[str, str]) -> None:
        for column in FIX_USAGE_COLUMNS:
            row[column] = ""

    _rewrite(results_csv, update, FIX_USAGE_COLUMNS)


def record_fix_usage(results_csv: Path, project: str, bug_id: str, response: FixResponse) -> None:
    """Persist provider-reported usage; unknown values remain empty, not zero."""
    def update(row: Dict[str, str]) -> None:
        if (row.get("project") or "").strip() == project and (row.get("id") or "").strip() == bug_id:
            for column in FIX_USAGE_COLUMNS:
                value = getattr(response, column)
                row[column] = "" if value is None else str(value)

    _rewrite(results_csv, update, FIX_USAGE_COLUMNS)


def record_time(results_csv: Path, project: str, bug_id: str, column: str, seconds: Optional[float]) -> None:
    """Set one bug's elapsed time (written immediately so an interrupted run keeps finished rows)."""

    def update(row: Dict[str, str]) -> None:
        if (row.get("project") or "").strip() == project and (row.get("id") or "").strip() == bug_id:
            row[column] = format_seconds(seconds)
            row[TOTAL_TIME_COL] = _total(row)

    _rewrite(results_csv, update)
