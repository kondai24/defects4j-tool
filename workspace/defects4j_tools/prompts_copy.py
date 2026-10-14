#!/usr/bin/env python3
"""Copy existing prompt files from one Defects4J workspace directory to another.

This is useful when you already generated prompts in one directory (from-dir)
(e.g., CoT-no-AST) and want to reuse them in another directory (to-dir)
(e.g., CoT-with-AST) without regenerating.

Rules:
- Iterates over rows in <to_dir>/results.csv (project,id,...).
- For each bug, resolves the bug Java file in both directories using
  `defects4j export -p classes.modified` + bug_locator.
- Copies "{ClassName}_prompt.md" next to the destination Java file.
- If the destination prompt already exists, prints its path and skips.
- If source checkout or prompt is missing, prints a message and skips.
- For copied prompts, prompt_time_sec in <from_dir>/results.csv (the generation time)
  is carried over to <to_dir>/results.csv, so copy time is never recorded as prompt time.
- Continues processing all rows; only fatal setup errors cause non-zero exit.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from . import bug_locator
from . import timing


def resolve_dir_path(value: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()

    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    # Fallback: resolve relative to this script directory.
    script_candidate = (Path(__file__).resolve().parent / raw).resolve()
    if script_candidate.exists():
        return script_candidate

    return cwd_candidate


def load_results_rows(results_csv: Path) -> List[Dict[str, str]]:
    with results_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"No headers found in {results_csv}")
        return list(reader)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy prompt files from --from-dir to --to-dir based on <to_dir>/results.csv.",
    )
    parser.add_argument("--from-dir", dest="from_dir", required=True, help="Directory that already has prompts")
    parser.add_argument("--to-dir", dest="to_dir", required=True, help="Directory to create prompt files in")
    return parser.parse_args()


def _prompt_path_for_java(java_file: Path) -> Path:
    return java_file.parent / f"{java_file.stem}_prompt.md"


def run(from_dir: str, to_dir: str) -> int:
    base_from = resolve_dir_path(from_dir)
    base_to = resolve_dir_path(to_dir)

    if not base_from.exists() or not base_from.is_dir():
        print(f"Error: --from-dir is not a directory: {base_from}", file=sys.stderr)
        return 2
    if not base_to.exists() or not base_to.is_dir():
        print(f"Error: --to-dir is not a directory: {base_to}", file=sys.stderr)
        return 2

    results_csv = base_to / "results.csv"
    if not results_csv.exists():
        print(f"Error: results.csv not found in --to-dir: {results_csv}", file=sys.stderr)
        return 2

    try:
        rows = load_results_rows(results_csv)
    except Exception as exc:
        print(f"Error: failed to read {results_csv}: {exc}", file=sys.stderr)
        return 2

    src_prompt_times = timing.read_column(base_from / "results.csv", timing.PROMPT_TIME_COL)

    copied: List[str] = []
    skipped_existing: List[str] = []

    missing_src_bug_dir: List[str] = []
    missing_src_prompt: List[str] = []

    missing_dst_bug_dir: List[str] = []
    missing_location: List[str] = []
    missing_java: List[str] = []
    copy_failed: List[str] = []

    total = len(rows)
    print(f"[info] prompts_copy: from={base_from} to={base_to}")
    print(f"[info] rows={total} (from {results_csv})")

    for idx, row in enumerate(rows, 1):
        project = (row.get("project") or "").strip()
        bug_id = (row.get("id") or "").strip()
        if not project or not bug_id:
            continue

        key = f"{project}-{bug_id}"
        prefix = f"[{idx}/{total}] {key}"

        dst_bug_dir = bug_locator.resolve_bug_dir(base_to, project, bug_id)
        if not dst_bug_dir:
            print(f"[miss] {prefix}: destination checkout not found")
            missing_dst_bug_dir.append(key)
            continue

        dst_location = bug_locator.run_defects4j_export_classes_modified(dst_bug_dir)
        if not dst_location:
            print(f"[miss] {prefix}: destination bug location not found (export failed)")
            missing_location.append(key)
            continue

        dst_java, _ = bug_locator.find_java_file(base_to, project, bug_id, dst_location)
        if dst_java is None:
            print(f"[miss] {prefix}: destination Java file not resolved (location={dst_location})")
            missing_java.append(key)
            continue

        dst_prompt = _prompt_path_for_java(dst_java)
        if dst_prompt.exists():
            print(f"[skip] {prefix}: prompt already exists: {dst_prompt}")
            skipped_existing.append(key)
            continue

        src_bug_dir = bug_locator.resolve_bug_dir(base_from, project, bug_id)
        if not src_bug_dir:
            print(f"[miss] {prefix}: source checkout not found")
            missing_src_bug_dir.append(key)
            continue

        src_location = bug_locator.run_defects4j_export_classes_modified(src_bug_dir)
        if not src_location:
            print(f"[miss] {prefix}: source bug location not found (export failed)")
            missing_location.append(key)
            continue

        src_java, _ = bug_locator.find_java_file(base_from, project, bug_id, src_location)
        if src_java is None:
            print(f"[miss] {prefix}: source Java file not resolved (location={src_location})")
            missing_java.append(key)
            continue

        src_prompt = _prompt_path_for_java(src_java)
        if not src_prompt.exists():
            print(f"[miss] {prefix}: source prompt not found: {src_prompt}")
            missing_src_prompt.append(key)
            continue

        try:
            dst_prompt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_prompt, dst_prompt)
            print(f"[copy] {prefix}: {src_prompt} -> {dst_prompt}")
            copied.append(key)
        except Exception as exc:
            print(f"[warn] {prefix}: copy failed: {exc}")
            copy_failed.append(key)
            continue

        src_time = src_prompt_times.get((project, bug_id), "")
        timing.record_time(results_csv, project, bug_id, timing.PROMPT_TIME_COL, float(src_time) if src_time else None)

    print("=" * 60)
    print("SUMMARY prompts_copy")
    print("=" * 60)
    print(f"Copied: {len(copied)}")
    print(f"Skipped (already exists): {len(skipped_existing)}")
    print(f"Missing source checkout: {len(missing_src_bug_dir)}")
    print(f"Missing source prompt: {len(missing_src_prompt)}")
    print(f"Missing destination checkout: {len(missing_dst_bug_dir)}")
    print(f"Missing bug location (export failed): {len(missing_location)}")
    print(f"Missing Java resolution: {len(missing_java)}")
    print(f"Copy failed: {len(copy_failed)}")

    if copied:
        print("\nCopied projects:")
        for item in copied:
            print(f"  - {item}")

    # Non-fatal issues are reported but do not fail the overall run.
    return 0


def main() -> None:
    args = parse_args()
    raise SystemExit(run(args.from_dir, args.to_dir))


if __name__ == "__main__":
    main()
