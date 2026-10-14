"""Fix provider backed by the OpenAI Codex SDK (`openai-codex`).

- Codex はファイルの読み取りもシェル経由で行うため、シェルは無効化できない。
- Docker 内では OS サンドボックス（bubblewrap）が動かないため Sandbox.full_access で実行し、
  cwd 外へのアクセスは実行ログ（TurnResult.items）から検出して警告する。
- 正解パッチを Web から探せないよう、web_search ツールは無効化する。
- 認証は環境変数 OPENAI_API_KEY を login_api_key() に渡して行う。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, List

from .fix_common import FIX_TIMEOUT_SEC, FixResponse, build_prompt, is_within, resolve_against

try:
    from openai_codex import ApprovalMode, AsyncCodex, Sandbox
except Exception:  # pragma: no cover
    AsyncCodex = None  # type: ignore[assignment]


# コマンド文字列中の絶対パス / `..` を含む相対パス
PATH_TOKEN_RE = re.compile(r"(?<![\w.~\-/])(/[^\s'\"`;|&<>(){}$]+|\.\.(?:/[^\s'\"`;|&<>(){}$]*)?)")
# 実行ファイルやデバイスなど、cwd 外でも正解の漏洩につながらないパス
IGNORED_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/", "/opt/venv/bin/", "/dev/")


def is_available() -> bool:
    return AsyncCodex is not None


def _unwrap(value: Any) -> Any:
    """pydantic の RootModel（ThreadItem や LegacyAppPathString）の中身を取り出す。"""
    return getattr(value, "root", value)


def _command_paths(command: str) -> List[str]:
    return [m.group(1) for m in PATH_TOKEN_RE.finditer(command)]


def _item_paths(item: Any, cwd: Path) -> Iterable[tuple]:
    """(種別, パス, 解決の基準ディレクトリ) を列挙する。"""
    root = _unwrap(item)
    item_type = getattr(root, "type", None)
    if item_type == "commandExecution":
        base = Path(str(_unwrap(root.cwd))) if root.cwd else cwd
        for action in root.command_actions or []:
            path = getattr(_unwrap(action), "path", None)
            if path:
                yield "command", str(_unwrap(path)), base
        for path in _command_paths(root.command):
            yield "command", path, base
    elif item_type == "fileChange":
        for change in root.changes:
            yield "fileChange", change.path, cwd


def find_outside_accesses(items: Iterable[Any], cwd: Path) -> List[str]:
    found: List[str] = []
    for item in items:
        for kind, raw, base in _item_paths(item, cwd):
            if raw.startswith(IGNORED_PREFIXES):
                continue
            resolved = resolve_against(raw, base)
            if is_within(resolved, cwd):
                continue
            entry = f"{kind}: {resolved}"
            if entry not in found:
                found.append(entry)
    return found


def _write_items_log(items: Iterable[Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            data = _unwrap(item).model_dump(mode="json", by_alias=True, warnings=False)
            fh.write(json.dumps(data, ensure_ascii=False))
            fh.write("\n")


async def _run_turn(*, cwd: Path, prompt: str, model: str):
    async with AsyncCodex() as codex:
        await codex.login_api_key(os.environ["OPENAI_API_KEY"])
        thread = await codex.thread_start(
            cwd=str(cwd),
            model=model,
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            ephemeral=True,
            config={"web_search": "disabled"},
        )
        return await thread.run(prompt)


async def run_fix(*, bug_cwd: Path, prompt_md: str, target_file: str, model: str, log_path: Path) -> FixResponse:
    cwd = bug_cwd.resolve()
    result = await asyncio.wait_for(
        _run_turn(cwd=cwd, prompt=build_prompt(prompt_md, target_file), model=model),
        timeout=FIX_TIMEOUT_SEC,
    )
    _write_items_log(result.items, log_path)
    if result.error is not None:
        raise RuntimeError(f"Codex turn failed ({result.status}): {result.error}")
    return FixResponse(content=result.final_response, outside_accesses=find_outside_accesses(result.items, cwd))
