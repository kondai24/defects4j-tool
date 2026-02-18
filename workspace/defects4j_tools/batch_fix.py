#!/usr/bin/env python3
"""Run Copilot-based fixes for checked-out Defects4J bugs.

Input: <target_dir>/results.csv (project,id,...)
Behavior:
- For each row, resolve the modified class via `defects4j export -p classes.modified`.
- Find the corresponding .java file (same algorithm as prompt generation).
- Look for a prompt file next to it: `{ClassName}_prompt.md`.
- If prompt is missing, log and skip.
- Otherwise, run Copilot (gpt-5-mini, streaming disabled) to edit the Java file.
- Verify whether the file changed via SHA-256 hash comparison and log the result.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from . import bug_locator

try:
    from copilot import CopilotClient
    from copilot.generated.session_events import SessionEventType
except Exception:  # pragma: no cover
    CopilotClient = None  # type: ignore[assignment]
    SessionEventType = None  # type: ignore[assignment]


# 出力はバッチ実行ログとして必要最小限にする
DEBUG_EVENT_DUMP = False
DEBUG_DELTA_STDOUT = False


IMPORTANT = set()
NOISY = set()
if SessionEventType is not None:
    IMPORTANT = {
        SessionEventType.ASSISTANT_MESSAGE_DELTA,
        SessionEventType.ASSISTANT_MESSAGE,
        SessionEventType.TOOL_EXECUTION_START,
        SessionEventType.TOOL_EXECUTION_COMPLETE,
        SessionEventType.SESSION_IDLE,
        SessionEventType.ASSISTANT_TURN_START,
        SessionEventType.ASSISTANT_TURN_END,
    }

    NOISY = {
        SessionEventType.SESSION_USAGE_INFO,
        SessionEventType.ASSISTANT_USAGE,
        SessionEventType.PENDING_MESSAGES_MODIFIED,
    }


def _safe_repr(obj, limit: int = 2000) -> str:
    try:
        s = repr(obj)
    except Exception:
        s = f"<unreprable {type(obj)}>"
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


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


async def _run_one_session(
    *,
    bug_cwd: Path,
    prompt_md: str,
    attachment_path: str,
    model: str,
    handle_event,
) -> Optional[str]:
    # Copilot SDK の client オプション `cwd` を使い、ツール実行時のパス解決を安定させる。
    client = CopilotClient({"cwd": str(bug_cwd)})
    await client.start()
    try:
        config = {
            "model": model,
            "streaming": False,
        }

        session = await client.create_session(config)
        session.on(handle_event)

        response = await session.send_and_wait(
            {
                "prompt": prompt_md,
                "attachments": [
                    {"type": "file", "path": attachment_path},
                ],
            },
            timeout=1800,
        )
        if response:
            return response.data.content
        return None
    finally:
        await client.stop()


def _write_fix_result(fh: TextIO, project: str, bug_id: str, content: str) -> None:
    fh.write(f"{project}_{bug_id}のcopilotの返答\n")
    fh.write(content)
    if not content.endswith("\n"):
        fh.write("\n")
    fh.write("\n")


async def _run_fix_for_one(
    idx: int,
    total: int,
    project: str,
    bug_id: str,
    java_file: Path,
    prompt_file: Path,
    model: str,
    results_fh: TextIO,
) -> str:
    label = f"{project}-{bug_id}"
    print(f"[{idx}/{total}] fixing {label}")

    if not prompt_file.exists():
        print(f"promptファイルが存在しません: {prompt_file.name}")
        print("promptが存在しないため、fixを実施しませんでした")
        _write_fix_result(results_fh, project, bug_id, "promptファイルが存在しません")
        return "skipped_no_prompt"

    bug_cwd = java_file.parent
    prompt_md = prompt_file.read_text(encoding="utf-8", errors="replace")
    original_bug = java_file.read_text(encoding="utf-8", errors="replace")
    original_hash = sha256_text(original_bug)

    # 何が起きているか可視化するイベントハンドラ
    def handle_event(event):
        # 通常実行ではイベントログは出さない
        return

    start = time.time()
    try:
        # 添付は、CopilotClient の cwd からの相対パスにする。
        # （通常は java_file は bug_cwd 配下のため relative_to が成功する）
        try:
            attachment_path = str(java_file.relative_to(bug_cwd))
        except ValueError:
            attachment_path = java_file.name

        content = await _run_one_session(
            bug_cwd=bug_cwd,
            prompt_md=prompt_md,
            attachment_path=attachment_path,
            model=model,
            handle_event=handle_event,
        )
    except (asyncio.TimeoutError, TimeoutError):
        print("タイムアウトのため、fixを実施できませんでした")
        _write_fix_result(results_fh, project, bug_id, "タイムアウトが発生しました")
        return "timeout"
    except Exception as exc:
        print(f"エラーのため、fixを実施できませんでした: {exc}")
        _write_fix_result(results_fh, project, bug_id, f"エラーが発生しました: {type(exc).__name__}: {exc}")
        return "error"
    elapsed = time.time() - start

    if content is None:
        content = "responseがNoneでした"
    _write_fix_result(results_fh, project, bug_id, content)

    updated_bug = java_file.read_text(encoding="utf-8", errors="replace")
    updated_hash = sha256_text(updated_bug)

    if updated_hash == original_hash:
        print("fixが実施されませんでした（編集されていない可能性があります）")
        return "unchanged"

    print(f"fixが実施されました（{elapsed:.0f}s）")
    return "updated"


async def _run_all(base_dir: Path, rows: List[Dict[str, str]], *, model: str) -> int:
    if CopilotClient is None:
        print("Error: Python package 'copilot' が見つかりません。", file=sys.stderr)
        print("まず 'copilot' クライアントが利用できる環境で実行してください。", file=sys.stderr)
        return 1

    total = sum(1 for r in rows if (r.get("project") or "").strip() and (r.get("id") or "").strip())
    if total == 0:
        print("No valid rows found in results.csv", file=sys.stderr)
        return 1

    counters: Dict[str, int] = {
        "updated": 0,
        "unchanged": 0,
        "timeout": 0,
        "error": 0,
        "skipped_no_prompt": 0,
        "skipped_no_bug_dir": 0,
        "skipped_no_location": 0,
        "skipped_no_java": 0,
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
                _write_fix_result(results_fh, project, bug_id, "bugディレクトリが見つからないため、fixを実施しませんでした")
                counters["skipped_no_bug_dir"] += 1
                print()
                continue

            bug_location = bug_locator.run_defects4j_export_classes_modified(bug_dir)
            if not bug_location:
                print(f"[{current}/{total}] fixing {label}")
                print("defects4j export に失敗したため、fixを実施しませんでした")
                _write_fix_result(results_fh, project, bug_id, "defects4j export に失敗したため、fixを実施しませんでした")
                counters["skipped_no_location"] += 1
                print()
                continue

            java_file, _ = bug_locator.find_java_file(base_dir, project, bug_id, bug_location)
            if java_file is None:
                print(f"[{current}/{total}] fixing {label}")
                print("バグファイル(.java)が特定できないため、fixを実施しませんでした")
                _write_fix_result(results_fh, project, bug_id, "バグファイル(.java)が特定できないため、fixを実施しませんでした")
                counters["skipped_no_java"] += 1
                print()
                continue

            prompt_file = java_file.parent / f"{java_file.stem}_prompt.md"
            status = await _run_fix_for_one(current, total, project, bug_id, java_file, prompt_file, model, results_fh)
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

    return 0


def run(target_dir: str, *, model: str = "gpt-5-mini") -> int:
    """Run fix for all rows in <target_dir>/results.csv."""

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
        return asyncio.run(_run_all(base_dir, rows, model=model))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
