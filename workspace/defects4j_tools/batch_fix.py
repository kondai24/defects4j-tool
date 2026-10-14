#!/usr/bin/env python3
"""Run LLM-based fixes (Claude / Codex) for checked-out Defects4J bugs.

Input: <target_dir>/results.csv (project,id,...)
Behavior:
- The provider is selected by the environment variable FIX_PROVIDER (claude | codex).
- For each row, resolve the modified class via `defects4j export -p classes.modified`.
- Find the corresponding .java file (same algorithm as prompt generation).
- Look for a prompt file next to it: `{ClassName}_prompt.md`.
- If prompt is missing, log and skip.
- Otherwise, run the provider with cwd = the Java file's directory to edit the Java file.
- Verify whether the file changed via SHA-256 hash comparison and log the result.
- Codex only: save the executed items to <target_dir>/codex_logs/ and warn about accesses outside cwd.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from . import bug_locator
from . import fix_claude
from . import fix_codex
from . import timing


PROVIDER_ENV = "FIX_PROVIDER"
PROVIDERS = {
    "claude": {"module": fix_claude, "api_key_env": "ANTHROPIC_API_KEY", "package": "claude-agent-sdk"},
    "codex": {"module": fix_codex, "api_key_env": "OPENAI_API_KEY", "package": "openai-codex"},
}


def get_provider() -> str:
    return (os.environ.get(PROVIDER_ENV) or "").strip()


def validate_config(model: Optional[str]) -> Optional[str]:
    """fix を始める前に provider / model / API キーを検証し、問題があればエラーメッセージを返す。"""
    provider = get_provider()
    if not provider:
        return f"環境変数 {PROVIDER_ENV} を指定してください（{' | '.join(PROVIDERS)}）。"
    if provider not in PROVIDERS:
        return f"{PROVIDER_ENV}={provider} は不正です。{' | '.join(PROVIDERS)} のいずれかを指定してください。"
    if not (model or "").strip():
        return "モデルを指定してください（fix は --model、plan モードは model）。"
    spec = PROVIDERS[provider]
    if not spec["module"].is_available():
        return f"Python package '{spec['package']}' が見つかりません。"
    if not (os.environ.get(spec["api_key_env"]) or "").strip():
        return f"環境変数 {spec['api_key_env']} を指定してください（{PROVIDER_ENV}={provider}）。"
    return None


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def resolve_dir_path(value: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()

    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    return cwd_candidate


def load_results_rows(results_csv: Path) -> List[Dict[str, str]]:
    with results_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"No headers found in {results_csv}")
        return list(reader)


def _write_fix_result(
    fh: TextIO, provider: str, project: str, bug_id: str, content: str, outside_accesses: Optional[List[str]] = None
) -> None:
    fh.write(f"{project}_{bug_id}の{provider}の返答\n")
    fh.write(content)
    if not content.endswith("\n"):
        fh.write("\n")
    if outside_accesses:
        fh.write("[警告] cwd 外へのアクセスを検出しました:\n")
        for entry in outside_accesses:
            fh.write(f"  - {entry}\n")
    fh.write("\n")


async def _run_provider(provider: str, *, bug_cwd: Path, prompt_md: str, target_file: str, model: str, log_path: Path):
    if provider == "codex":
        return await fix_codex.run_fix(
            bug_cwd=bug_cwd, prompt_md=prompt_md, target_file=target_file, model=model, log_path=log_path
        )
    return await fix_claude.run_fix(bug_cwd=bug_cwd, prompt_md=prompt_md, target_file=target_file, model=model)


async def _run_fix_for_one(
    idx: int,
    total: int,
    provider: str,
    project: str,
    bug_id: str,
    java_file: Path,
    prompt_file: Path,
    model: str,
    results_fh: TextIO,
    results_csv: Path,
    counters: Dict[str, int],
) -> str:
    label = f"{project}-{bug_id}"
    print(f"[{idx}/{total}] fixing {label}")

    if not prompt_file.exists():
        print(f"promptファイルが存在しません: {prompt_file.name}")
        print("promptが存在しないため、fixを実施しませんでした")
        _write_fix_result(results_fh, provider, project, bug_id, "promptファイルが存在しません")
        return "skipped_no_prompt"

    bug_cwd = java_file.parent
    prompt_md = prompt_file.read_text(encoding="utf-8", errors="replace")
    original_bug = java_file.read_text(encoding="utf-8", errors="replace")
    original_hash = sha256_text(original_bug)
    log_path = results_csv.parent / "codex_logs" / f"{project}_{bug_id}.jsonl"

    start = time.perf_counter()
    try:
        response = await _run_provider(
            provider,
            bug_cwd=bug_cwd,
            prompt_md=prompt_md,
            target_file=java_file.name,
            model=model,
            log_path=log_path,
        )
    except (asyncio.TimeoutError, TimeoutError):
        print("タイムアウトのため、fixを実施できませんでした")
        _write_fix_result(results_fh, provider, project, bug_id, "タイムアウトが発生しました")
        return "timeout"
    except Exception as exc:
        print(f"エラーのため、fixを実施できませんでした: {exc}")
        _write_fix_result(
            results_fh, provider, project, bug_id, f"エラーが発生しました: {type(exc).__name__}: {exc}"
        )
        return "error"
    finally:
        # タイムアウト・エラー時も、fix セッションにかかった時間を記録する
        elapsed = time.perf_counter() - start
        timing.record_time(results_csv, project, bug_id, timing.FIX_TIME_COL, elapsed)

    content = response.content if response.content is not None else "responseがNoneでした"
    _write_fix_result(results_fh, provider, project, bug_id, content, response.outside_accesses)
    if response.outside_accesses:
        counters["outside_access"] += 1
        print(f"[警告] cwd 外へのアクセスを検出しました（{len(response.outside_accesses)} 件、詳細は fix_results.txt）")

    updated_bug = java_file.read_text(encoding="utf-8", errors="replace")
    updated_hash = sha256_text(updated_bug)

    if updated_hash == original_hash:
        print("fixが実施されませんでした（編集されていない可能性があります）")
        return "unchanged"

    print(f"fixが実施されました（{elapsed:.0f}s）")
    return "updated"


async def _run_all(
    base_dir: Path, results_csv: Path, rows: List[Dict[str, str]], *, provider: str, model: str
) -> int:
    total = sum(1 for r in rows if (r.get("project") or "").strip() and (r.get("id") or "").strip())
    if total == 0:
        print("No valid rows found in results.csv", file=sys.stderr)
        return 1

    timing.clear_column(results_csv, timing.FIX_TIME_COL)
    print(f"provider: {provider} / model: {model}")

    counters: Dict[str, int] = {
        "updated": 0,
        "unchanged": 0,
        "timeout": 0,
        "error": 0,
        "skipped_no_prompt": 0,
        "skipped_no_bug_dir": 0,
        "skipped_no_location": 0,
        "skipped_no_java": 0,
        "outside_access": 0,
    }

    results_path = base_dir / "fix_results.txt"
    with results_path.open("w", encoding="utf-8", newline="\n") as results_fh:
        current = 0
        for row in rows:
            project = (row.get("project") or "").strip()
            bug_id = (row.get("id") or "").strip()
            if not project or not bug_id:
                continue

            current += 1
            label = f"{project}-{bug_id}"

            bug_dir = bug_locator.resolve_bug_dir(base_dir, project, bug_id)
            if not bug_dir:
                print(f"[{current}/{total}] fixing {label}")
                print("bugディレクトリが見つからないため、fixを実施しませんでした")
                _write_fix_result(results_fh, provider, project, bug_id, "bugディレクトリが見つからないため、fixを実施しませんでした")
                counters["skipped_no_bug_dir"] += 1
                print()
                continue

            bug_location = bug_locator.run_defects4j_export_classes_modified(bug_dir)
            if not bug_location:
                print(f"[{current}/{total}] fixing {label}")
                print("defects4j export に失敗したため、fixを実施しませんでした")
                _write_fix_result(results_fh, provider, project, bug_id, "defects4j export に失敗したため、fixを実施しませんでした")
                counters["skipped_no_location"] += 1
                print()
                continue

            java_file, _ = bug_locator.find_java_file(base_dir, project, bug_id, bug_location)
            if java_file is None:
                print(f"[{current}/{total}] fixing {label}")
                print("バグファイル(.java)が特定できないため、fixを実施しませんでした")
                _write_fix_result(results_fh, provider, project, bug_id, "バグファイル(.java)が特定できないため、fixを実施しませんでした")
                counters["skipped_no_java"] += 1
                print()
                continue

            prompt_file = java_file.parent / f"{java_file.stem}_prompt.md"
            status = await _run_fix_for_one(
                current,
                total,
                provider,
                project,
                bug_id,
                java_file,
                prompt_file,
                model,
                results_fh,
                results_csv,
                counters,
            )
            counters[status] = counters.get(status, 0) + 1
            print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total: {total}")
    print(f"Updated: {counters['updated']}")
    print(f"Unchanged: {counters['unchanged']}")
    print(f"Skipped (no prompt): {counters['skipped_no_prompt']}")
    print(f"Skipped (no bug dir): {counters['skipped_no_bug_dir']}")
    print(f"Skipped (no location): {counters['skipped_no_location']}")
    print(f"Skipped (no java): {counters['skipped_no_java']}")
    print(f"Timeout: {counters['timeout']}")
    print(f"Error: {counters['error']}")
    if provider == "codex":
        print(f"Outside cwd access (warning): {counters['outside_access']}")

    return 0


def run(target_dir: str, *, model: Optional[str]) -> int:
    """Run fix for all rows in <target_dir>/results.csv."""

    error = validate_config(model)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    base_dir = resolve_dir_path(target_dir)
    results_csv = base_dir / "results.csv"
    if not results_csv.exists():
        print(f"Error: results.csv not found: {results_csv}", file=sys.stderr)
        return 1

    try:
        rows = load_results_rows(results_csv)
    except Exception as exc:
        print(f"Error: results.csv の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_run_all(base_dir, results_csv, rows, provider=get_provider(), model=model))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
