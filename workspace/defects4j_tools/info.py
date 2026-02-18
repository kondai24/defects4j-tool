#!/usr/bin/env python3
"""
Extract the bug report URL from `defects4j info -p <project> -b <bug>` output.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print the bug report URL for a Defects4J project bug."
    )
    parser.add_argument("project", help="Defects4J project id (e.g., Lang)")
    parser.add_argument("bug_id", help="Bug identifier (e.g., 1)")
    return parser.parse_args()


def run_defects4j(project: str, bug_id: str) -> str:
    command = ["defects4j", "info", "-p", project, "-b", bug_id]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("Error: `defects4j` command not found in PATH.", file=sys.stderr)
        sys.exit(127)

    if result.returncode != 0:
        print(result.stderr.strip() or "defects4j command failed.", file=sys.stderr)
        sys.exit(result.returncode)

    return result.stdout


def extract_bug_url(info_output: str) -> Optional[str]:
    pattern = re.compile(r"Bug report url:\s*(\S+)", re.IGNORECASE)
    match = pattern.search(info_output)
    if match:
        return match.group(1)
    return None


def main() -> None:
    args = parse_args()
    info_output = run_defects4j(args.project, args.bug_id)
    bug_url = extract_bug_url(info_output)
    if not bug_url:
        print("Bug report URL not found in defects4j output.", file=sys.stderr)
        sys.exit(1)
    print(bug_url)


if __name__ == "__main__":
    main()
