"""Shared types and helpers for the Claude / Codex fix providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# 1 バグあたりの fix セッションの上限時間（Copilot 実装と同じ値）
FIX_TIMEOUT_SEC = 1800


@dataclass
class FixResponse:
    content: Optional[str]
    # cwd 外へのアクセス（Codex のみ検出。Claude はフックで拒否するため常に空）
    outside_accesses: List[str] = field(default_factory=list)
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class FixSessionError(RuntimeError):
    """Provider failure with any usage reported before the failure."""

    def __init__(self, message: str, response: FixResponse):
        super().__init__(message)
        self.response = response


def build_prompt(prompt_md: str, target_file: str) -> str:
    """Copilot の添付ファイルの代わりに、修正対象ファイルを prompt 末尾で明示する。"""
    return (
        f"{prompt_md.rstrip()}\n\n"
        "## Target File\n\n"
        f"The file to fix is `{target_file}` in the current working directory. "
        "Read it and edit it in place.\n"
    )


def resolve_against(path: str, cwd: Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)
