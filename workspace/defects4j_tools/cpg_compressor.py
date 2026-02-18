"""CFG+PDG compressor reused from CoT-with-DFG/cpg_compressor_v10.py.

The logic is preserved; only CLI glue is removed so it can be imported.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_left
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import networkx as nx

JAVA_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "break", "continue",
    "return", "throw", "try", "catch", "finally", "new", "this", "super",
    "null", "true", "false", "class", "public", "protected", "private", "static",
    "final", "void", "int", "boolean", "byte", "char", "short", "long", "float", "double"
}

NOISE_VAR_PATTERNS = [
    re.compile(r"^obj\d*$"),
    re.compile(r"^\$stack\d*$"),
    re.compile(r"^\$obj\d*$"),
    re.compile(r"^temp\d*$"),
]
NOISE_VAR_EXACT = {
    "empty", "at", "return", "<init>", "<clinit>", "this", "RET"
}


def _norm_attr(v, default=""):
    if v is None:
        return default
    if isinstance(v, list):
        v = v[0] if v else default
    return str(v).strip('"')


def _read_dot_any(path: str):
    if os.path.isdir(path):
        graphs = []
        for root, _, files in os.walk(path):
            for f in files:
                if f.endswith(".dot"):
                    fp = os.path.join(root, f)
                    try:
                        g = nx.drawing.nx_pydot.read_dot(fp)
                        graphs.append(g[0] if isinstance(g, list) else g)
                    except Exception as e:
                        print(f"Warning: could not read {fp}: {e}")
        if not graphs:
            raise FileNotFoundError(f"No .dot files in {path}")
        G = nx.MultiDiGraph()
        for g in graphs:
            G = nx.compose(G, g)
        return G
    g = nx.drawing.nx_pydot.read_dot(path)
    return g[0] if isinstance(g, list) else g


def _short(s: str, n: int = 18) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + ".."


def _is_noise_var(v: str) -> bool:
    if not v:
        return True
    if v.startswith("$"):
        return True
    if "<" in v or ">" in v:
        return True
    if v in JAVA_KEYWORDS:
        return True
    if v in NOISE_VAR_EXACT:
        return True
    for pat in NOISE_VAR_PATTERNS:
        if pat.match(v):
            return True
    if v[0].isupper() and "." not in v and not v.isupper():
        return True
    return False


def _extract_first_identifier(code: str) -> str:
    code = (code or "").strip()
    m = re.search(r"\bthis\.[A-Za-z_][A-Za-z0-9_]*\b", code)
    if m:
        return m.group(0)
    for m in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", code):
        tok = m.group(0)
        if not _is_noise_var(tok):
            return tok
    return ""


def _normalize_var_display(var: str, context_code: str) -> str:
    var = (var or "").strip()
    if not var or "." in var:
        return var
    if f"this.{var}" in (context_code or ""):
        return f"this.{var}"
    return var


def _parse_def(code: str) -> Optional[Tuple[str, str]]:
    s = (code or "").strip().rstrip(";").strip()
    if not s:
        return None
    if any(op in s for op in ["==", "!=", "<=", ">="]):
        return None
    if s.startswith(("if ", "while ", "for ", "return ", "throw ")):
        return None

    m = re.match(r"^(?P<lhs>this\.[A-Za-z_]\w*|[A-Za-z_]\w*)\s*(?P<op>\+=|-=|\*=|/=|%=|=)\s*(?P<rhs>.+)$", s)
    if m:
        lhs, op, rhs = m.group("lhs"), m.group("op"), m.group("rhs").strip()
        if op != "=":
            rhs = f"{lhs} {op} {rhs}"
        return lhs, rhs

    if "=" in s:
        parts = s.split("=", 1)
        lhs_part = parts[0].strip()
        rhs_part = parts[1].strip()
        lhs_tokens = lhs_part.split()
        if lhs_tokens:
            var_name = lhs_tokens[-1]
            if not _is_noise_var(var_name):
                return var_name, rhs_part
    return None


def _pretty_method_sig(code: str) -> str:
    s = (code or "").strip()
    if not s:
        return "<?>"
    m = re.search(r"([A-Za-z_]\w*)\s*\((.*)\)", s)
    if not m:
        return _short(s, 60)
    name, params = m.group(1), m.group(2).strip()
    if not params:
        return f"{name}()"
    parts = []
    for p in params.split(","):
        p = re.sub(r"\bfinal\b", "", p).strip()
        toks = p.split()
        if len(toks) >= 2:
            parts.append(" ".join(toks[:-1]))
        else:
            parts.append(toks[0])
    return f"{name}({','.join(p.replace(' ', '') for p in parts)})"


def compress_cfg_pdg(dot_path: str, max_code_len: int = 90, max_defs_per_use: int = 3) -> str:
    G = _read_dot_any(dot_path)

    nodes: Dict[str, dict] = {}
    nodes_by_line: Dict[int, List[dict]] = defaultdict(list)
    line_ctrl_type: Dict[int, str] = {}

    for node_id, attrs in G.nodes(data=True):
        label = _norm_attr(attrs.get("label", ""))
        line_s = _norm_attr(attrs.get("LINE_NUMBER", ""))
        name = _norm_attr(attrs.get("NAME", ""))
        code = _norm_attr(attrs.get("CODE", ""))
        full_name = _norm_attr(attrs.get("FULL_NAME", ""))
        method_full_name = _norm_attr(attrs.get("METHOD_FULL_NAME", ""))
        ctrl_type = _norm_attr(attrs.get("CONTROL_STRUCTURE_TYPE", ""))

        if not line_s or line_s == "<empty>":
            continue
        try:
            line = int(line_s)
        except Exception:
            continue

        if label == "CONTROL_STRUCTURE" and ctrl_type:
            line_ctrl_type[line] = ctrl_type

        if code and len(code) > max_code_len:
            code = code[:max_code_len] + ".."

        info = {
            "id": node_id,
            "label": label,
            "line": line,
            "name": name,
            "code": code,
            "full_name": full_name,
            "method_full_name": method_full_name,
            "order": int(_norm_attr(attrs.get("ORDER", "0"))),
        }
        nodes[node_id] = info
        nodes_by_line[line].append(info)

    priority = {"CONTROL_STRUCTURE": 0, "CALL": 1, "RETURN": 2, "JUMP_TARGET": 3, "METHOD": 4}
    reps: Dict[int, dict] = {}
    for ln, infos in nodes_by_line.items():
        reps[ln] = sorted(infos, key=lambda x: (priority.get(x["label"], 99), x["order"]))[0]

    method_lines = sorted([ln for ln, r in reps.items() if r["label"] == "METHOD"])
    method_sig_to_line: Dict[str, int] = {}
    method_name_to_lines: Dict[str, List[int]] = defaultdict(list)

    methods = []
    for ln in method_lines:
        r = reps[ln]
        m_info = {
            "line": ln,
            "code": r["code"],
            "sig": _pretty_method_sig(r["code"]),
            "full_name": r["full_name"],
            "name": r["name"],
        }
        methods.append(m_info)

        if r["full_name"]:
            method_sig_to_line[r["full_name"]] = ln
        if r["name"]:
            method_name_to_lines[r["name"]].append(ln)

    def _method_for_line(ln: int) -> Optional[int]:
        if not method_lines:
            return None
        i = bisect_left(method_lines, ln)
        if i < len(method_lines) and method_lines[i] == ln:
            return ln
        if i == 0:
            return None
        return method_lines[i - 1]

    line_to_method = {ln: _method_for_line(ln) for ln in reps}

    ctor_lines = [m["line"] for m in methods if "<init>" in m["name"] or m["name"] == m["sig"].split("(")[0]]
    primary_ctor_line = ctor_lines[0] if ctor_lines else None

    init_lines: List[int] = []
    if method_lines:
        first_method = method_lines[0]
        for ln in sorted(reps.keys()):
            if ln < first_method:
                code = reps[ln]["code"]
                if "this." in code and "=" in code:
                    init_lines.append(ln)
                    if primary_ctor_line:
                        line_to_method[ln] = primary_ctor_line
    init_lines_set = set(init_lines)

    cfg_succ = defaultdict(lambda: defaultdict(list))
    cdg_by = defaultdict(lambda: defaultdict(set))
    use_to_defs = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))

    def_to_use = True

    for src, dst, attrs in G.edges(data=True):
        if src not in nodes or dst not in nodes:
            continue
        s, d = nodes[src], nodes[dst]
        s_line, d_line = s["line"], d["line"]
        label = _norm_attr(attrs.get("label", ""))
        u_label = label.upper()

        if u_label in {"CFG", "CFG_NEXT", "TRUE", "FALSE"}:
            if s["label"] == "METHOD" or d["label"] == "METHOD":
                continue
            ms, md = line_to_method.get(s_line), line_to_method.get(d_line)
            if ms and md and ms == md and s_line != d_line:
                tag = ""
                if "TRUE" in u_label:
                    tag = "(True)"
                elif "FALSE" in u_label:
                    tag = "(False)"
                elif "ELSE" in u_label:
                    tag = "(Else)"
                cfg_succ[ms][s_line].append((d_line, tag))

        elif label == "CDG":
            ms, md = line_to_method.get(s_line), line_to_method.get(d_line)
            if ms and md and ms == md:
                cdg_by[md][d_line].add(f"L{s_line}")

        elif label in {"REACHING_DEF", "DDG"}:
            def_node, use_node = (s, d) if def_to_use else (d, s)
            if def_node["line"] == use_node["line"]:
                continue
            m_use = line_to_method.get(use_node["line"])
            if not m_use:
                continue
            var = use_node["name"] or _extract_first_identifier(use_node["code"])
            var = _normalize_var_display(var, use_node["code"])
            if _is_noise_var(var):
                continue
            use_rep = reps[use_node["line"]]
            if use_rep["label"] == "CALL" and (f"{var}(" in (use_rep["code"] or "") or f".{var}" in (use_rep["code"] or "")):
                continue
            use_to_defs[m_use][use_node["line"]][var].add(def_node["line"])

    for m in cfg_succ:
        for s_line, targets in cfg_succ[m].items():
            if len(targets) == 2 and all(t[1] == "" for t in targets):
                c_type = line_ctrl_type.get(s_line, "IF")
                t1, t2 = sorted(targets, key=lambda x: x[0])

                if c_type == "DO":
                    if t1[0] <= s_line:
                        cfg_succ[m][s_line] = [(t1[0], "(True)"), (t2[0], "(False)")]
                    else:
                        cfg_succ[m][s_line] = [(t1[0], "(True)"), (t2[0], "(False)")]
                else:
                    cfg_succ[m][s_line] = [(t1[0], "(True)"), (t2[0], "(False)")]

    def _get_call_target_info(ln: int, rep: dict) -> str:
        if rep["label"] != "CALL":
            return ""
        line_nodes = nodes_by_line[ln]
        call_node = next((n for n in line_nodes if n["label"] == "CALL"), None)

        if call_node and call_node["method_full_name"]:
            target = call_node["method_full_name"]
            if target in method_sig_to_line:
                t_line = method_sig_to_line[target]
                short_name = target.split(":")[0].split(".")[-1]
                if t_line != ln:
                    return f"CALLS: {short_name} at L{t_line}"

        simple_name = rep["name"]
        call_code = (rep["code"] or "")
        if simple_name in method_name_to_lines:
            candidates = method_name_to_lines[simple_name]
            if len(candidates) == 1:
                t_line = candidates[0]
                if t_line != ln:
                    if f".{simple_name}" in call_code and not f"this.{simple_name}" in call_code:
                        pass
                    else:
                        return f"CALLS: {simple_name} at L{t_line}"
        return ""

    out_lines: List[str] = []

    for m in methods:
        ml = m["line"]
        out_lines.append(f"[METHOD L{ml}: {m['sig']}]")

        body_lines = sorted([l for l in reps if line_to_method.get(l) == ml and l != ml and l not in init_lines_set])

        if primary_ctor_line == ml and init_lines:
            out_lines.append("  INIT:")
            for ln in init_lines:
                rep = reps[ln]
                code = rep["code"]
                pd = _parse_def(code)
                text = f"DEF {pd[0]} := {pd[1]}" if pd else code
                anns = []
                succs = cfg_succ[ml][ln]
                if succs:
                    s_parts = [f"{t[0]}{t[1]}" for t in sorted(succs, key=lambda x: x[0])]
                    anns.append(f"Flow: -> [{', '.join(s_parts)}]")
                call_link = _get_call_target_info(ln, rep)
                if call_link:
                    anns.append(call_link)
                for var, defs in use_to_defs[ml][ln].items():
                    d_str = ", ".join(f"L{d}" for d in sorted(defs)[-max_defs_per_use:])
                    anns.append(f"USE {var} <- DEF at {d_str}")
                comment = (" // " + " | ".join(anns)) if anns else ""
                out_lines.append(f"    [L{ln:<3}] {text}{comment}")

        if not body_lines and not (primary_ctor_line == ml and init_lines):
            out_lines.append("  (no printable statements)")

        if body_lines:
            out_lines.append("  BODY:")
            for ln in body_lines:
                rep = reps[ln]
                code = rep["code"]
                kind = rep["label"]

                if kind == "CONTROL_STRUCTURE":
                    text = f"CTRL {code}"
                elif kind == "RETURN":
                    text = f"RETURN {code}"
                elif kind == "CALL":
                    pd = _parse_def(code)
                    if pd:
                        text = f"DEF {pd[0]} := {pd[1]}"
                    else:
                        text = f"CALL {code}"
                else:
                    pd = _parse_def(code)
                    text = f"DEF {pd[0]} := {pd[1]}" if pd else f"{kind} {code}"

                anns = []
                succs = cfg_succ[ml][ln]
                if succs:
                    s_parts = [f"{t[0]}{t[1]}" for t in sorted(succs, key=lambda x: x[0])]
                    anns.append(f"Flow: -> [{', '.join(s_parts)}]")
                elif kind in {"RETURN", "THROW"}:
                    anns.append("Flow: -> [EXIT]")

                if ln in cdg_by[ml]:
                    anns.append(f"CtrlBy: [{', '.join(sorted(cdg_by[ml][ln]))}]")

                call_link = _get_call_target_info(ln, rep)
                if call_link:
                    anns.append(call_link)

                for var, defs in use_to_defs[ml][ln].items():
                    d_str = ", ".join(f"L{d}" for d in sorted(defs)[-max_defs_per_use:])
                    anns.append(f"USE {var} <- DEF at {d_str}")

                comment = (" // " + " | ".join(anns)) if anns else ""
                out_lines.append(f"    [L{ln:<3}] {text}{comment}")
        out_lines.append("")

    return "\n".join(out_lines).rstrip() + "\n"


__all__ = ["compress_cfg_pdg"]
