"""Fix provider backed by the Claude Agent SDK.

- 使えるツールは Read / Edit / Write / Glob / Grep のみ（Bash なし）。
- cwd 外のパスに触れるツール呼び出しは PreToolUse フックで拒否する。
- permission_mode="dontAsk" により、フックで許可されなかった承認待ちの呼び出しも自動で拒否される。
- 認証は環境変数 ANTHROPIC_API_KEY を使う。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fix_common import FIX_TIMEOUT_SEC, FixResponse, FixSessionError, build_prompt, is_within, resolve_against

try:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
except Exception:  # pragma: no cover
    query = None  # type: ignore[assignment]


TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
GLOB_MAGIC = set("*?[{")


def is_available() -> bool:
    return query is not None


def _glob_static_prefix(pattern: str) -> str:
    """`src/**/*.java` -> `src`、`/abs/dir/*.java` -> `/abs/dir`"""
    parts: List[str] = []
    for part in Path(pattern).parts:
        if any(ch in GLOB_MAGIC for ch in part):
            break
        parts.append(part)
    return str(Path(*parts)) if parts else "."


def _paths_to_check(tool_name: str, tool_input: Dict[str, Any]) -> List[str]:
    if tool_name in ("Read", "Edit", "Write"):
        return [str(tool_input.get("file_path") or "")]
    if tool_name == "Glob":
        base = str(tool_input.get("path") or ".")
        pattern = str(tool_input.get("pattern") or "")
        return [base, str(Path(base) / _glob_static_prefix(pattern))]
    if tool_name == "Grep":
        return [str(tool_input.get("path") or ".")]
    return []


def outside_paths(tool_name: str, tool_input: Dict[str, Any], cwd: Path) -> List[str]:
    """ツール呼び出しが触れる cwd 外のパスを返す（cwd 内なら空）。"""
    if tool_name not in TOOLS:
        return [f"<{tool_name}>"]
    return [p for p in _paths_to_check(tool_name, tool_input) if not is_within(resolve_against(p, cwd), cwd)]


def _make_path_guard(cwd: Path):
    async def guard(input_data, tool_use_id, context):
        outside = outside_paths(input_data["tool_name"], input_data.get("tool_input") or {}, cwd)
        if outside:
            decision, reason = "deny", f"Access outside the working directory is not allowed: {', '.join(outside)}"
        else:
            decision, reason = "allow", "Path is inside the working directory"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }

    return guard


async def _collect_result(prompt: str, options: "ClaudeAgentOptions") -> FixResponse:
    result: Optional[ResultMessage] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return FixResponse(content=None)
    usage = result.usage or {}
    response = FixResponse(
        content=result.result,
        total_cost_usd=result.total_cost_usd,
        num_turns=result.num_turns,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )
    if result.is_error:
        detail = "; ".join(result.errors or []) or (result.result or "")
        raise FixSessionError(f"Claude session failed ({result.subtype}): {detail}", response)
    return response


async def run_fix(*, bug_cwd: Path, prompt_md: str, target_file: str, model: str,
                  effort: Optional[str] = None) -> FixResponse:
    cwd = bug_cwd.resolve()
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        model=model,
        effort=effort,
        # fix セッション内・セッション間ともにプロンプトキャッシュを使わない
        env={"DISABLE_PROMPT_CACHING": "1"},
        tools=TOOLS,
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_make_path_guard(cwd)])]},
        # ユーザー / プロジェクトの設定・CLAUDE.md・skills を読み込まず、実験条件を固定する
        setting_sources=[],
        skills=[],
    )
    return await asyncio.wait_for(
        _collect_result(build_prompt(prompt_md, target_file), options),
        timeout=FIX_TIMEOUT_SEC,
    )
