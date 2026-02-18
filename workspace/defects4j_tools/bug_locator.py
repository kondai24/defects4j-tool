#!/usr/bin/env python3
"""Bug-file resolution helpers for Defects4J workspace scripts.

This module centralizes the logic used by both prompt generation and fix automation:

- Determine modified class ("bug location") via `defects4j export -p classes.modified`.
- Resolve the corresponding Java source file inside the checked-out bug directory.

The dataset assumption used by callers: one bug corresponds to one Java file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def run_defects4j_export_classes_modified(bug_dir: Path) -> Optional[str]:
    """Return the last non-empty line from `defects4j export -p classes.modified`.

    Returns None if the command fails.
    """

    cmd = ["defects4j", "export", "-p", "classes.modified"]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(bug_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip()
        print(f"[warn] export failed in {bug_dir}: {msg}", file=sys.stderr)
        return None

    lines = result.stdout.splitlines()
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return None


def resolve_bug_dir(base_dir: Path, project: str, bug_id: str) -> Optional[Path]:
    """Resolve bug checkout directory under base_dir."""

    search_patterns = [
        f"{project}-{bug_id}",
        f"{project.lower()}-{bug_id}b",
        f"{project.lower()}-{bug_id}",
    ]
    for pattern in search_patterns:
        candidate = base_dir / pattern
        if candidate.exists():
            return candidate
    return None


def parse_fqcn(value: str) -> Optional[Tuple[str, str]]:
    value = value.strip()
    if not value or value.startswith("ERROR:"):
        return None
    if "/" in value or value.endswith(".java"):
        return None
    if "." not in value:
        return None
    parts = [p for p in value.split(".") if p]
    if len(parts) < 2:
        return None
    package = ".".join(parts[:-1])
    class_name = parts[-1]
    return package, class_name


def read_java_package_decl(java_file: Path) -> Optional[str]:
    try:
        with java_file.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(80):
                line = fh.readline()
                if not line:
                    return None
                stripped = line.strip()
                if stripped.startswith("package ") and stripped.endswith(";"):
                    return stripped.removeprefix("package ").removesuffix(";").strip()
    except Exception:
        return None
    return None


def find_java_file(base_dir: Path, project: str, bug_id: str, bug_location: str) -> Tuple[Optional[Path], Optional[Path]]:
    """Resolve the target Java file.

    Returns (java_file_path, bug_dir_path).
    """

    bug_dir = resolve_bug_dir(base_dir, project, bug_id)
    if not bug_dir:
        return None, None

    bug_location = (bug_location or "").strip()
    if not bug_location or bug_location.startswith("ERROR:"):
        return None, bug_dir

    src_roots = ["source", "src/main/java", "src/java", "src"]

    # If bug_location already looks like a path, try it as relative to bug_dir.
    if "/" in bug_location or bug_location.endswith(".java"):
        raw = bug_location.lstrip("./")
        candidate = (bug_dir / raw).resolve()
        if candidate.exists() and candidate.suffix == ".java":
            return candidate, bug_dir

    fqcn = parse_fqcn(bug_location)
    if not fqcn:
        return None, bug_dir

    package, class_name = fqcn
    rel = Path(*package.split(".")) / f"{class_name}.java"

    # Exact match by package path first (avoids same-class-name collisions).
    for root in src_roots:
        candidate = bug_dir / root / rel
        if candidate.exists():
            return candidate, bug_dir

    # Fallback: rglob by class name within known roots, then disambiguate by package decl.
    matches: List[Path] = []
    for root in src_roots:
        src_dir = bug_dir / root
        if not src_dir.exists():
            continue
        matches.extend(src_dir.rglob(f"{class_name}.java"))

    if not matches:
        return None, bug_dir
    if len(matches) == 1:
        return matches[0], bug_dir

    expected_pkg = package
    pkg_matches = [m for m in matches if read_java_package_decl(m) == expected_pkg]
    if len(pkg_matches) == 1:
        return pkg_matches[0], bug_dir

    return None, bug_dir
