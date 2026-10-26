from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

try:
    from tree_sitter_languages import get_parser
except ImportError:
    get_parser = None  # type: ignore[assignment]


def _require_tree_sitter_languages() -> None:
    if get_parser is None:
        raise SystemExit(
            "Error: mode=AST には tree_sitter_languages が必要です。"
            "Defects4J/venv_hg を使うか、pip install tree-sitter-languages を実行してください。"
        )


def _get_java_parser():
    # tree_sitter_languages 1.10.2 は内部で非推奨の Language(path, name) を呼ぶため、
    # tree-sitter 0.21.x が FutureWarning を出す。文法バージョンを変えると AST 出力が
    # 変わり得るため、ライブラリは固定したままこの警告だけを抑制する。
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Language\(path, name\) is deprecated",
            category=FutureWarning,
        )
        return get_parser("java")


def parse_java_to_sexp(java_code: str) -> str:
    _require_tree_sitter_languages()
    parser = _get_java_parser()
    tree = parser.parse(java_code.encode("utf-8"))
    return tree.root_node.sexp()


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def pretty_sexp(sexp: str, indent: int = 2) -> str:
    out = []
    level = 0
    i = 0
    n = len(sexp)
    in_string = False
    escape = False

    def newline():
        if out and out[-1] != "\n":
            out.append("\n")
        out.append(" " * (level * indent))

    while i < n:
        ch = sexp[i]

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "(":
            if out and out[-1] not in ["\n", " "]:
                newline()
            out.append("(")
            level += 1
            i += 1
            continue

        if ch == ")":
            level -= 1
            if out and out[-1] != "\n":
                newline()
            out.append(")")
            i += 1
            continue

        if ch.isspace():
            if out and out[-1] not in [" ", "\n"]:
                out.append(" ")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out).strip() + "\n"


def java_file_to_pretty_sexp(java_file: Path, indent: int = 2) -> Optional[str]:
    try:
        code = read_text_file(java_file)
    except Exception:
        return None

    try:
        sexp = parse_java_to_sexp(code)
        return pretty_sexp(sexp, indent=indent)
    except Exception:
        return None
