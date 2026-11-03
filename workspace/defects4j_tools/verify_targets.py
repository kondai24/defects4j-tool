"""Find the methods to compare in the verify step.

- Defects4J's ``<id>.src.patch`` is a diff from the fixed (correct) version to the buggy version:
  ``-`` lines belong to the correct version and ``+`` lines to the buggy version.
- A target method is a method that contains a changed line in either version and exists in both
  versions (methods added or removed by the developer cannot be compared directly).
- Java sources are parsed with tree-sitter (already used by the AST prompt mode).
"""

from __future__ import annotations

import difflib
import re
import warnings
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# src.patch
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int  # 1-based line in the correct version
    new_start: int  # 1-based line in the buggy version
    lines: List[Tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text)

    def changed_lines(self) -> Tuple[Set[int], Set[int]]:
        """Return (correct-side numbers of '-' lines, buggy-side numbers of '+' lines)."""
        old_no, new_no = self.old_start, self.new_start
        old_changed: Set[int] = set()
        new_changed: Set[int] = set()
        for tag, _ in self.lines:
            if tag == " ":
                old_no += 1
                new_no += 1
            elif tag == "-":
                old_changed.add(old_no)
                old_no += 1
            elif tag == "+":
                new_changed.add(new_no)
                new_no += 1
        return old_changed, new_changed


def parse_unified_diff(text: str) -> Dict[str, List[Hunk]]:
    """Parse a unified diff into {path relative to the checkout: hunks} (``a/`` prefix removed)."""
    files: Dict[str, List[Hunk]] = {}
    current: Optional[List[Hunk]] = None
    hunk: Optional[Hunk] = None
    for raw in text.splitlines():
        if raw.startswith("--- "):
            hunk = None
            continue
        if raw.startswith("+++ "):
            path = raw[4:].split("\t")[0].strip()
            if path.startswith("b/"):
                path = path[2:]
            current = files.setdefault(path, [])
            continue
        m = _HUNK_RE.match(raw)
        if m and current is not None:
            hunk = Hunk(old_start=int(m.group(1)), new_start=int(m.group(3)))
            current.append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        tag = raw[:1] if raw[:1] in (" ", "-", "+") else " "
        hunk.lines.append((tag, raw[1:]))
    return files


def apply_hunks(correct_text: str, hunks: Sequence[Hunk]) -> str:
    """Apply the correct->buggy hunks to the correct source, giving the buggy source."""
    lines = correct_text.splitlines(keepends=True)
    out: List[str] = []
    pos = 0  # 0-based index into lines
    for hunk in hunks:
        start = hunk.old_start - 1 if hunk.old_start > 0 else 0
        out.extend(lines[pos:start])
        pos = start
        for tag, text in hunk.lines:
            if tag == " ":
                out.append(lines[pos])
                pos += 1
            elif tag == "-":
                if lines[pos].rstrip("\r\n") != text.rstrip("\r\n"):
                    raise ValueError(f"patch does not match the correct source at line {pos + 1}")
                pos += 1
            else:
                out.append(text + "\n")
    out.extend(lines[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Java methods
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JavaMethod:
    class_name: str  # binary name, e.g. org.apache.commons.lang3.StringUtils or a.b.Outer$Inner
    name: str  # "<init>" for constructors
    param_types: Tuple[str, ...]  # source types without generics, e.g. ("int", "String[]")
    return_type: str  # "void" for constructors
    is_static: bool
    is_constructor: bool
    start_line: int  # 1-based, inclusive
    end_line: int
    in_anonymous_or_local: bool = False

    @property
    def key(self) -> Tuple[str, str, Tuple[str, ...]]:
        return (self.class_name, self.name, self.param_types)

    @property
    def display(self) -> str:
        return f"{self.class_name}.{self.name}({', '.join(self.param_types)})"


def _java_parser():
    from tree_sitter_languages import get_parser

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"Language\(path, name\) is deprecated", category=FutureWarning)
        return get_parser("java")


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _normalize_type(text: str) -> str:
    # Drop annotations and generics, keep array brackets: "final List<String> []" -> "List[]".
    text = re.sub(r"@\w+(\.\w+)*(\([^)]*\))?", "", text)
    depth = 0
    out = []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


_TYPE_DECLS = {"class_declaration", "interface_declaration", "enum_declaration", "annotation_type_declaration",
               "record_declaration"}


def extract_methods(java_text: str) -> List[JavaMethod]:
    """Return every method and constructor declared in the file (including nested classes)."""
    src = java_text.encode("utf-8")
    tree = _java_parser().parse(src)
    root = tree.root_node
    package = ""
    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    package = _text(sub, src)
    methods: List[JavaMethod] = []

    def visit(node, class_name: Optional[str], anonymous: bool) -> None:
        for child in node.children:
            if child.type in _TYPE_DECLS:
                name_node = child.child_by_field_name("name")
                simple = _text(name_node, src) if name_node else "?"
                if class_name is None:
                    binary = f"{package}.{simple}" if package else simple
                else:
                    binary = f"{class_name}${simple}"
                visit(child, binary, anonymous)
            elif child.type in ("method_declaration", "constructor_declaration") and class_name:
                methods.append(_method(child, class_name, anonymous))
                visit(child, class_name, True)  # anonymous/local classes inside the body
            elif child.type == "object_creation_expression" and child.child_by_field_name("body") is not None:
                visit(child, class_name, True)
            else:
                visit(child, class_name, anonymous)

    def _method(node, class_name: str, anonymous: bool) -> JavaMethod:
        is_ctor = node.type == "constructor_declaration"
        modifiers = ""
        for sub in node.children:
            if sub.type == "modifiers":
                modifiers = _text(sub, src)
        params: List[str] = []
        plist = node.child_by_field_name("parameters")
        if plist is not None:
            for p in plist.children:
                if p.type == "formal_parameter":
                    t = p.child_by_field_name("type")
                    dims = p.child_by_field_name("dimensions")
                    params.append(_normalize_type(_text(t, src)) + (_text(dims, src).replace(" ", "") if dims else ""))
                elif p.type == "spread_parameter":
                    type_node = next((c for c in p.children if c.type not in ("modifiers", "...", "variable_declarator")), None)
                    params.append(_normalize_type(_text(type_node, src)) + "[]")
        if is_ctor:
            ret = "void"
        else:
            t = node.child_by_field_name("type")
            dims = node.child_by_field_name("dimensions")
            ret = _normalize_type(_text(t, src)) + (_text(dims, src).replace(" ", "") if dims else "")
        name_node = node.child_by_field_name("name")
        return JavaMethod(
            class_name=class_name,
            name="<init>" if is_ctor else _text(name_node, src),
            param_types=tuple(params),
            return_type=ret,
            is_static=bool(re.search(r"\bstatic\b", modifiers)),
            is_constructor=is_ctor,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            in_anonymous_or_local=anonymous,
        )

    visit(root, None, False)
    return methods


def methods_touching(methods: Iterable[JavaMethod], lines: Iterable[int]) -> List[JavaMethod]:
    """Innermost declared methods that contain any of the lines."""
    hits: Dict[Tuple, JavaMethod] = {}
    line_set = set(lines)
    ms = list(methods)
    for line in line_set:
        enclosing = [m for m in ms if m.start_line <= line <= m.end_line]
        if enclosing:
            inner = min(enclosing, key=lambda m: m.end_line - m.start_line)
            hits[inner.key] = inner
    return sorted(hits.values(), key=lambda m: m.start_line)


def lines_outside_methods(methods: Iterable[JavaMethod], lines: Iterable[int]) -> List[int]:
    ms = list(methods)
    return sorted(l for l in set(lines) if not any(m.start_line <= l <= m.end_line for m in ms))


@dataclass
class TargetReport:
    targets: List[JavaMethod]  # methods in the correct version to compare
    added_by_developer: List[str]  # only in the correct version
    removed_by_developer: List[str]  # only in the buggy version
    outside_methods: Dict[str, List[int]]  # path -> correct-side lines changed outside any method


def find_targets(correct_sources: Dict[str, str], patch: Dict[str, List[Hunk]]) -> Tuple[TargetReport, Dict[str, str]]:
    """Return the target report and the reconstructed buggy sources ({path: text}) of patched files."""
    targets: Dict[Tuple, JavaMethod] = {}
    added: Set[str] = set()
    removed: Set[str] = set()
    outside: Dict[str, List[int]] = {}
    buggy_sources: Dict[str, str] = {}
    for path, hunks in patch.items():
        if not path.endswith(".java"):
            continue
        correct = correct_sources[path]
        buggy = apply_hunks(correct, hunks)
        buggy_sources[path] = buggy
        old_lines: Set[int] = set()
        new_lines: Set[int] = set()
        for h in hunks:
            o, n = h.changed_lines()
            old_lines |= o
            new_lines |= n
        correct_methods = extract_methods(correct)
        buggy_methods = extract_methods(buggy)
        correct_keys = {m.key: m for m in correct_methods}
        buggy_keys = {m.key: m for m in buggy_methods}
        touched = {m.key: m for m in methods_touching(correct_methods, old_lines)}
        for m in methods_touching(buggy_methods, new_lines):
            touched.setdefault(m.key, correct_keys.get(m.key, m))
        for key, m in touched.items():
            if key in correct_keys and key in buggy_keys:
                if correct_keys[key].in_anonymous_or_local:
                    continue  # lambda/anonymous bodies are compared through their enclosing method
                targets[key] = correct_keys[key]
            elif key in correct_keys:
                added.add(m.display)
            else:
                removed.add(m.display)
        rest = lines_outside_methods(correct_methods, old_lines)
        if rest:
            outside[path] = rest
    report = TargetReport(
        targets=sorted(targets.values(), key=lambda m: (m.class_name, m.start_line)),
        added_by_developer=sorted(added),
        removed_by_developer=sorted(removed),
        outside_methods=outside,
    )
    return report, buggy_sources


def changed_methods(before: str, after: str) -> Tuple[List[JavaMethod], bool]:
    """Methods whose lines differ between ``before`` and ``after``; flag = non-blank changes outside methods."""
    a_lines = before.splitlines()
    b_lines = after.splitlines()
    before_changed: Set[int] = set()
    after_changed: Set[int] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False).get_opcodes():
        if tag != "equal":
            before_changed.update(range(i1 + 1, i2 + 1))
            after_changed.update(range(j1 + 1, j2 + 1))
    after_methods = extract_methods(after)
    before_methods = extract_methods(before)
    hits = {m.key: m for m in methods_touching(after_methods, after_changed)}
    for m in methods_touching(before_methods, before_changed):
        hits.setdefault(m.key, m)
    outside = [l for l in lines_outside_methods(after_methods, after_changed) if b_lines[l - 1].strip()]
    outside += [l for l in lines_outside_methods(before_methods, before_changed) if a_lines[l - 1].strip()]
    return sorted(hits.values(), key=lambda m: (m.class_name, m.start_line)), bool(outside)


# ---------------------------------------------------------------------------
# Signature support (decision 6) and JVM descriptors
# ---------------------------------------------------------------------------

SUPPORTED_PARAM_TYPES = {"int", "boolean", "String", "int[]", "boolean[]", "String[]"}

_PRIMITIVE_DESC = {"int": "I", "boolean": "Z", "long": "J", "short": "S", "byte": "B", "char": "C",
                   "float": "F", "double": "D", "void": "V"}
_BOXED = {"Integer", "Long", "Short", "Byte", "Character", "Boolean", "Float", "Double", "String"}
SUPPORTED_RETURN_ELEMENTS = set(_PRIMITIVE_DESC) - {"void"} | _BOXED


def _strip_java_lang(t: str) -> str:
    return t[len("java.lang."):] if t.startswith("java.lang.") else t


def descriptor_of(java_type: str) -> Optional[str]:
    t = _strip_java_lang(java_type)
    dims = 0
    while t.endswith("[]"):
        dims += 1
        t = t[:-2]
    if t in _PRIMITIVE_DESC:
        base = _PRIMITIVE_DESC[t]
    elif t in _BOXED:
        base = f"Ljava/lang/{t};"
    else:
        return None
    if base == "V" and dims:
        return None
    return "[" * dims + base


def method_descriptor(m: JavaMethod) -> Optional[str]:
    params = [descriptor_of(p) for p in m.param_types]
    ret = descriptor_of(m.return_type)
    if ret is None or any(p is None for p in params):
        return None
    return "(" + "".join(params) + ")" + ret  # type: ignore[arg-type]


def unsupported_reason(m: JavaMethod) -> Optional[str]:
    """None when the method can be compared; otherwise a short explanation for result.json."""
    if m.is_constructor:
        return "constructor"
    if not m.is_static:
        return "instance method"
    bad = [p for p in m.param_types if _strip_java_lang(p) not in SUPPORTED_PARAM_TYPES]
    if bad:
        return "unsupported parameter type: " + ", ".join(bad)
    ret = _strip_java_lang(m.return_type)
    if ret != "void":
        elem = ret[:-2] if ret.endswith("[]") else ret
        if elem.endswith("[]") or elem not in SUPPORTED_RETURN_ELEMENTS:
            return "unsupported return type: " + m.return_type
    return None
