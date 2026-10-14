#!/usr/bin/env python3
"""Generate Defects4J bug-fix prompts.

- Input is <target_dir>/results.csv (project,id,...).
- For each row, this script re-exports bug location via `defects4j export -p classes.modified`.
- Bug title/description are scraped ONLY for Apache JIRA issues (issues.apache.org).
- Prompts are saved next to the target Java file as "{ClassName}_prompt.md".
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import info
from . import bug_locator
from . import cpg_compressor as compressor
from . import java_to_ast_sexp
from . import timing

APACHE_PREFIX = "https://issues.apache.org"
DEFAULT_BASE_DIR = "CoT-no-AST"
USER_AGENT = "Mozilla/5.0 (compatible; PromptGenerator/1.0; +https://github.com/)"
SCRIPT_DIR = Path(__file__).resolve().parent

 


PROMPT_FILES: Dict[str, str] = {
    "normal": "normal.txt",
    "AST": "ast.txt",
    "CFG_PDG": "cfg_pdg.txt",
}

_TEMPLATE_CACHE: Dict[str, str] = {}


def load_prompt_template(mode: str) -> str:
    key = mode if mode in PROMPT_FILES else mode.upper()
    filename = PROMPT_FILES.get(key) or PROMPT_FILES.get(key.upper())
    if not filename:
        raise ValueError(f"Unknown prompt mode: {mode}")
    path = SCRIPT_DIR / "prompts" / filename
    if key not in _TEMPLATE_CACHE:
        _TEMPLATE_CACHE[key] = path.read_text(encoding="utf-8")
    return _TEMPLATE_CACHE[key]

class JiraBugParser(HTMLParser):
    """Pull the first <h1> text and user-content-block body from a Jira issue page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h1 = False
        self._h1_done = False
        self._h1_parts: List[str] = []

        self._capture_div = False
        self._div_depth = 0
        self._div_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        if tag == "h1" and not self._h1_done:
            self._in_h1 = True
            return

        if tag == "div":
            if self._capture_div:
                self._div_depth += 1
                return

            attr_dict = dict(attrs)
            class_value = attr_dict.get("class", "")
            if "user-content-block" in class_value.lower():
                self._capture_div = True
                self._div_depth = 1
                return

        if self._capture_div and tag in {"p", "li"}:
            self._div_parts.append("\n")
        elif self._capture_div and tag == "br":
            self._div_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        if self._capture_div and tag == "br":
            self._div_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True
            return

        if self._capture_div and tag == "div":
            self._div_depth -= 1
            if self._div_depth == 0:
                self._capture_div = False
            return

        if self._capture_div and tag in {"p", "li"}:
            self._div_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self._h1_parts.append(data)
        if self._capture_div:
            self._div_parts.append(data)

    def title(self) -> str:
        return normalize_text("".join(self._h1_parts))

    def body(self) -> str:
        return normalize_text("".join(self._div_parts))


def normalize_text(text: str) -> str:
    """Collapse whitespace while keeping blank lines between paragraphs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    cleaned: List[str] = []
    blank_pending = False
    for line in lines:
        if not line:
            blank_pending = True
            continue
        if blank_pending and cleaned:
            cleaned.append("")
        cleaned.append(line)
        blank_pending = False
    return "\n".join(cleaned).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate prompts from <target_dir>/results.csv.")
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=DEFAULT_BASE_DIR,
        help="Target directory that contains <project>-<id>/ folders and results.csv (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["normal", "AST", "CFG_PDG"],
        default="normal",
        help="Prompt generation mode (default: %(default)s)",
    )
    return parser.parse_args()


def fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def scrape_jira(url: str) -> Tuple[str, str]:
    html = fetch_html(url)
    parser = JiraBugParser()
    parser.feed(html)
    parser.close()
    title = parser.title() or "(no title)"
    body = parser.body() or "(no description)"
    return title, body


def fill_template_normal(title: str, description: str) -> str:
    prompt = load_prompt_template("normal").replace("{{ bug title }}", title)
    prompt = prompt.replace("{{ Bug Title }}", title)
    prompt = prompt.replace("{{ bug description }}", description)
    prompt = prompt.replace("{{ Bug Description }}", description)
    return prompt


def fill_template_ast(title: str, description: str, ast: str) -> str:
    prompt = load_prompt_template("AST").replace("{{ bug title }}", title)
    prompt = prompt.replace("{{ Bug Title }}", title)
    prompt = prompt.replace("{{ bug description }}", description)
    prompt = prompt.replace("{{ Bug Description }}", description)
    prompt = prompt.replace("{{ AST }}", ast)
    prompt = prompt.replace("{{ ast }}", ast)
    return prompt


def fill_template_cfg_pdg(title: str, description: str, cfg_pdg: str) -> str:
    prompt = load_prompt_template("CFG_PDG").replace("{{ bug title }}", title)
    prompt = prompt.replace("{{ Bug Title }}", title)
    prompt = prompt.replace("{{ bug description }}", description)
    prompt = prompt.replace("{{ Bug Description }}", description)
    prompt = prompt.replace("{{ CFG_PDG_DATA }}", cfg_pdg)
    prompt = prompt.replace("{{ cfg_pdg_data }}", cfg_pdg)
    return prompt


def resolve_data_path(value: str) -> Path:
    """Resolve a data-file path, preferring cwd but falling back to the script directory."""
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()

    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    script_candidate = (SCRIPT_DIR / raw).resolve()
    if script_candidate.exists():
        return script_candidate

    return cwd_candidate


def resolve_dir_path(value: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()

    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    script_candidate = (SCRIPT_DIR / raw).resolve()
    if script_candidate.exists():
        return script_candidate

    return cwd_candidate


def load_results_rows(results_csv: Path) -> List[Dict[str, str]]:
    with results_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"No headers found in {results_csv}")
        return list(reader)


# --- AST prompt generator (tree-sitter S-expression; same algorithm as sample/java_to_ast.py) ---


def _require_tree_sitter_languages(mode: str) -> None:
    if mode != "AST":
        return
    java_to_ast_sexp._require_tree_sitter_languages()


def load_bug_locations_fallback(base_dir: Path) -> Dict[Tuple[str, str], str]:
    """Load bug locations from base-dir/prompts.csv if present (fallback for main CSVs without bug location)."""

    candidates = [base_dir / "prompts.csv", base_dir / "more-than-4-hunks.csv"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with candidate.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                locations: Dict[Tuple[str, str], str] = {}
                for row in reader:
                    project = (row.get("project") or "").strip()
                    bug_id = (row.get("id") or "").strip()
                    location = (row.get("bug location") or "").strip()
                    if project and bug_id and location:
                        locations[(project, bug_id)] = location
                return locations
        except Exception as exc:
            print(f"[warn] Failed to load bug locations from {candidate}: {exc}", file=sys.stderr)
            return {}
    return {}




def read_java_code_utf8sig(java_file: Path) -> Optional[str]:
    try:
        return java_to_ast_sexp.read_text_file(java_file)
    except Exception as exc:
        print(f"[warn] Failed to read {java_file}: {exc}", file=sys.stderr)
        return None


def generate_cfg_pdg(java_file: Path) -> Optional[str]:
    cpg_dir = java_file.parent / "cpg"

    if cpg_dir.exists():
        try:
            shutil.rmtree(cpg_dir)
        except Exception as exc:
            print(f"[warn] Failed to clean cpg dir for {java_file}: {exc}", file=sys.stderr)
            return None

    try:
        cpg_dir.mkdir(parents=False, exist_ok=False)
    except Exception as exc:
        print(f"[warn] Failed to create cpg dir for {java_file}: {exc}", file=sys.stderr)
        return None

    cpg_bin = cpg_dir / "cpg.bin"
    graph_dir = cpg_dir / "graph_output"
    # joern-export often expects the output directory to NOT exist.
    # Avoid pre-creating it; if it exists from a previous run, remove it.
    if graph_dir.exists():
        try:
            shutil.rmtree(graph_dir)
        except Exception as exc:
            print(f"[warn] Failed to clean graph_output dir for {java_file}: {exc}", file=sys.stderr)
            return None

    cmd_parse = [
        "joern-parse",
        str(java_file),
        "--language",
        "JAVASRC",
        "-o",
        str(cpg_bin),
    ]
    try:
        subprocess.run(cmd_parse, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip()
        print(f"[warn] joern-parse failed for {java_file}: {msg}", file=sys.stderr)
        return None

    cmd_export = [
        "joern-export",
        str(cpg_bin),
        "--repr",
        "all",
        "--format",
        "dot",
        "-o",
        str(graph_dir),
    ]
    try:
        subprocess.run(cmd_export, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip()
        print(f"[warn] joern-export failed for {java_file}: {msg}", file=sys.stderr)
        return None

    dot_path = graph_dir / "export.dot"
    if not dot_path.exists():
        print(f"[warn] export.dot not found for {java_file} at {dot_path}", file=sys.stderr)
        return None

    compressed_path = cpg_dir / "compressed.txt"
    try:
        compressed_text = compressor.compress_cfg_pdg(str(dot_path))
    except Exception as exc:
        print(f"[warn] cpg compression failed for {java_file}: {exc}", file=sys.stderr)
        return None

    try:
        compressed_path.write_text(compressed_text, encoding="utf-8")
    except Exception as exc:
        print(f"[warn] Failed to write compressed.txt for {java_file}: {exc}", file=sys.stderr)
        return None

    return compressed_text


def cleanup_cfg_pdg_artifacts(java_file: Path) -> None:
    """Remove temporary artifacts created during CFG/PDG generation."""
    cpg_dir = java_file.parent / "cpg"
    if not cpg_dir.exists():
        return
    try:
        shutil.rmtree(cpg_dir)
    except Exception as exc:
        print(f"[warn] Failed to cleanup cpg dir for {java_file}: {exc}", file=sys.stderr)


def write_prompt_to_file(java_file: Path, prompt: str) -> bool:
    try:
        class_name = java_file.stem
        prompt_file = java_file.parent / f"{class_name}_prompt.md"
        with prompt_file.open("w", encoding="utf-8") as fh:
            fh.write(prompt)
        return True
    except Exception as exc:
        print(f"[warn] Failed to write prompt next to {java_file}: {exc}", file=sys.stderr)
        return False


def format_location_cell(base_dir: Path, java_file: Optional[Path], fallback_text: str) -> str:
    if java_file is None:
        return fallback_text
    try:
        rel = java_file.relative_to(base_dir).as_posix()
    except Exception:
        return fallback_text
    display = java_file.name
    return f"[{display}](./{rel})"


def write_bugs_info_md(base_dir: Path, rows: List[Dict[str, str]]) -> None:
    out_path = base_dir / "bugs-info.md"
    header = "| project | id | location |\n| --- | ---: | --- |\n"
    lines = [header]
    for row in rows:
        project = row.get("project", "").strip()
        bug_id = row.get("id", "").strip()
        location = row.get("location", "").strip()
        lines.append(f"| {project} | {bug_id} | {location} |\n")
    out_path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_dir = resolve_dir_path(args.target_dir)
    _require_tree_sitter_languages(args.mode)

    results_csv = base_dir / "results.csv"
    if not results_csv.exists():
        sys.exit(f"Error: results.csv not found: {results_csv}")

    rows = load_results_rows(results_csv)
    timing.clear_column(results_csv, timing.PROMPT_TIME_COL)

    jira_cache: Dict[str, Tuple[str, str]] = {}

    generated = 0
    skipped_non_jira = 0
    skipped_no_url = 0
    skipped_no_bug_dir = 0
    skipped_no_code = 0
    skipped_no_location = 0
    skipped_cfg_pdg_failed = 0

    total = len(rows)
    for idx, row in enumerate(rows, 1):
        project = (row.get("project") or "").strip()
        bug_id = (row.get("id") or "").strip()
        if not project or not bug_id:
            continue

        bug_dir = base_dir / f"{project}-{bug_id}"
        if not bug_dir.exists():
            skipped_no_bug_dir += 1
            continue

        bug_location = bug_locator.run_defects4j_export_classes_modified(bug_dir)
        if not bug_location:
            skipped_no_location += 1
            continue

        java_file, _bug_dir = bug_locator.find_java_file(base_dir, project, bug_id, bug_location)
        if java_file is None:
            skipped_no_code += 1
            continue

        # Measured from here: bug-location lookup above is dataset bookkeeping.
        started = time.perf_counter()
        info_output = info.run_defects4j(project, bug_id)
        url = (info.extract_bug_url(info_output) or "").strip()
        if not url or url == "UNKNOWN":
            skipped_no_url += 1
            continue
        if not url.startswith(APACHE_PREFIX):
            skipped_non_jira += 1
            continue

        if url not in jira_cache:
            try:
                jira_cache[url] = scrape_jira(url)
            except URLError as exc:
                print(f"[warn] Failed to fetch {url}: {exc}", file=sys.stderr)
                jira_cache[url] = ("(no title)", "(no description)")
            except Exception as exc:
                print(f"[warn] Unexpected error while parsing {url}: {exc}", file=sys.stderr)
                jira_cache[url] = ("(no title)", "(no description)")

        title, body = jira_cache[url]

        try:
            if args.mode == "normal":
                prompt = fill_template_normal(title, body)
            elif args.mode == "AST":
                java_code = read_java_code_utf8sig(java_file)
                if java_code is None:
                    skipped_no_code += 1
                    continue
                sexp = java_to_ast_sexp.parse_java_to_sexp(java_code)
                pretty = java_to_ast_sexp.pretty_sexp(sexp, indent=2)
                prompt = fill_template_ast(title, body, pretty)
            elif args.mode == "CFG_PDG":
                print(
                    f"[info] Generating CFG/PDG: {project}-{bug_id} ({java_file.name})",
                    file=sys.stderr,
                )
                cfg_pdg = generate_cfg_pdg(java_file)
                if cfg_pdg is None:
                    print(
                        f"[warn] CFG/PDG generation failed: {project}-{bug_id}",
                        file=sys.stderr,
                    )
                    skipped_cfg_pdg_failed += 1
                    continue
                prompt = fill_template_cfg_pdg(title, body, cfg_pdg)
            else:
                raise ValueError(f"Unknown mode: {args.mode}")

            if write_prompt_to_file(java_file, prompt):
                elapsed = time.perf_counter() - started
                timing.record_time(results_csv, project, bug_id, timing.PROMPT_TIME_COL, elapsed)
                generated += 1
                print(f"[info] Generated prompt: {project}-{bug_id}", file=sys.stderr)
        finally:
            if args.mode == "CFG_PDG":
                cleanup_cfg_pdg_artifacts(java_file)

        if idx % 25 == 0 or idx == total:
            print(f"[info] Progress {idx}/{total}", file=sys.stderr)

    print(
        "[info] Done: "
        f"generated={generated}, skipped_no_url={skipped_no_url}, "
        f"skipped_non_jira={skipped_non_jira}, skipped_no_location={skipped_no_location}, "
        f"skipped_no_bug_dir={skipped_no_bug_dir}, skipped_no_code={skipped_no_code}, "
        f"skipped_cfg_pdg_failed={skipped_cfg_pdg_failed}.",
        file=sys.stderr,
    )


def run(target_dir: str, mode: str = "normal") -> int:
    """Run prompt generation for a checked-out target directory.

    target_dir: directory containing results.csv and <project>-<id>/ folders.
    mode: "normal", "AST", or "CFG_PDG".
    """
    argv_backup = sys.argv
    try:
        sys.argv = [argv_backup[0], target_dir, "--mode", mode]
        main()
        return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.strip():
            print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = argv_backup


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
