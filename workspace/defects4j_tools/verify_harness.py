"""Generate the JBMC comparison harness for one target method.

The harness entry method takes the same parameters as the target method. JBMC gives every entry
parameter an arbitrary (nondeterministic) value, including ``null`` for String/array parameters.
Both versions receive the same values (arrays are copied, so a version that writes to its
argument cannot influence the other). The harness asserts:

1. both versions throw an exception of the same class, or both return normally
   (``CProver.classIdentifier`` is used because ``getClass()`` is not modelled precisely), and
2. when both return normally, the return values are equal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .verify_targets import JavaMethod

HARNESS_CLASS = "VerifyHarness"
ENTRY_METHOD = "check"
CORRECT_PREFIX = "jbmcverify/correct"  # package prefix of the relocated correct version

EXCEPTION_ASSERT_MARK = "verify:exception"
RETURN_ASSERT_MARK = "verify:return"

_INTEGRAL = {"int", "long", "short", "byte", "char", "boolean"}
_FLOATING = {"float", "double"}
_BOXED_TO_PRIMITIVE = {"Integer": "int", "Long": "long", "Short": "short", "Byte": "byte", "Character": "char",
                       "Boolean": "boolean", "Float": "float", "Double": "double"}


@dataclass
class Harness:
    source: str
    exception_line: int  # line of the exception assertion (to map JBMC properties)
    return_line: int  # line of the return-value assertion (0 for void)


def _strip(t: str) -> str:
    return t[len("java.lang."):] if t.startswith("java.lang.") else t


def _source_class_name(binary_name: str) -> str:
    return binary_name.replace("$", ".")


def _equal_expr(java_type: str, a: str, b: str) -> str:
    t = _strip(java_type)
    if t.endswith("[]"):
        return f"eq({a}, {b})"
    if t in _INTEGRAL:
        return f"{a} == {b}"
    if t in _FLOATING:
        return f"({a} == {b} || ({a} != {a} && {b} != {b}))"
    return f"eq({a}, {b})"


def _helpers(return_type: str) -> List[str]:
    t = _strip(return_type)
    if t == "void":
        return []
    elem = t[:-2] if t.endswith("[]") else t
    lines: List[str] = []
    if elem == "String":
        lines += [
            "    private static boolean eq(String a, String b) {",
            "        return a == null ? b == null : (b != null && a.equals(b));",
            "    }",
        ]
    elif elem in _BOXED_TO_PRIMITIVE:
        prim = _BOXED_TO_PRIMITIVE[elem]
        lines += [
            f"    private static boolean eq({elem} a, {elem} b) {{",
            "        if (a == null || b == null) return a == b;",
            f"        {prim} x = a; {prim} y = b;",
            f"        return {_equal_expr(prim, 'x', 'y')};",
            "    }",
        ]
    if t.endswith("[]"):
        lines += [
            f"    private static boolean eq({elem}[] a, {elem}[] b) {{",
            "        if (a == null || b == null) return a == b;",
            "        if (a.length != b.length) return false;",
            "        for (int i = 0; i < a.length; i++) {",
            f"            if (!({_equal_expr(elem, 'a[i]', 'b[i]')})) return false;",
            "        }",
            "        return true;",
            "    }",
        ]
    return lines


def java_literal(java_type: str, value: Any) -> str:
    """Java source literal for a counterexample value of a supported parameter type."""
    t = _strip(java_type)
    if value is None:
        if t in ("int", "boolean"):
            raise ValueError(f"null for primitive {t}")
        return "null"
    if t.endswith("[]"):
        elem = t[:-2]
        return f"new {elem}[] {{" + ", ".join(java_literal(elem, v) for v in value) + "}"
    if t == "int":
        v = int(value)
        return "Integer.MIN_VALUE" if v == -(1 << 31) else str(v)
    if t == "boolean":
        return "true" if value else "false"
    if t == "String":
        if all(32 <= ord(c) < 127 and c not in '"\\' for c in value):
            return '"' + value + '"'
        # \uXXXX escapes are processed before lexing, so build unusual strings from char codes.
        return "new String(new char[] {" + ", ".join(str(ord(c)) for c in value) + "})"
    raise ValueError(f"unsupported type {java_type}")


def _exclusion(method: JavaMethod, values: Dict[str, Any]) -> str:
    conds = []
    for i, t in enumerate(method.param_types):
        name, t = f"p{i}", _strip(t)
        v = values[name]
        if t in ("int", "boolean"):
            conds.append(f"{name} == {java_literal(t, v)}")
        elif t == "String":
            conds.append(f"{name} == null" if v is None else f"({name} != null && {name}.equals({java_literal(t, v)}))")
        else:
            raise ValueError("exclusions support int, boolean and String parameters only")
    return "        CProver.assume(!(" + (" && ".join(conds) or "true") + "));"


def can_exclude(method: JavaMethod) -> bool:
    return all(_strip(t) in ("int", "boolean", "String") for t in method.param_types)


def generate(method: JavaMethod, exclusions: Sequence[Dict[str, Any]] = ()) -> Harness:
    """Harness for JBMC; ``exclusions`` are inputs already shown to be spurious counterexamples."""
    params = [f"{_strip(t)} p{i}" for i, t in enumerate(method.param_types)]
    ret = _strip(method.return_type)
    correct_cls = CORRECT_PREFIX.replace("/", ".") + "." + _source_class_name(method.class_name)
    fixed_cls = _source_class_name(method.class_name)

    def args(version: str) -> str:
        out = []
        for i, t in enumerate(method.param_types):
            out.append(f"p{i}_{version}" if _strip(t).endswith("[]") else f"p{i}")
        return ", ".join(out)

    body: List[str] = [_exclusion(method, values) for values in exclusions]
    for i, t in enumerate(method.param_types):
        t = _strip(t)
        if t.endswith("[]"):
            for v in ("c", "f"):
                body.append(f"        {t} p{i}_{v} = p{i} == null ? null : p{i}.clone();")
    if ret != "void":
        default = "null" if ret.endswith("[]") or ret not in (_INTEGRAL | _FLOATING) else (
            "false" if ret == "boolean" else "0")
        body.append(f"        {ret} rc = {default};")
        body.append(f"        {ret} rf = {default};")
    body.append("        Throwable ec = null;")
    body.append("        Throwable ef = null;")
    call_c = f"{correct_cls}.{method.name}({args('c')})"
    call_f = f"{fixed_cls}.{method.name}({args('f')})"
    if ret != "void":
        body.append(f"        try {{ rc = {call_c}; }} catch (Throwable t) {{ ec = t; }}")
        body.append(f"        try {{ rf = {call_f}; }} catch (Throwable t) {{ ef = t; }}")
    else:
        body.append(f"        try {{ {call_c}; }} catch (Throwable t) {{ ec = t; }}")
        body.append(f"        try {{ {call_f}; }} catch (Throwable t) {{ ef = t; }}")

    header = [
        "// Generated by defects4j_tools.verify_harness. Do not edit.",
        f"// target: {method.display} -> {method.return_type}",
        "import org.cprover.CProver;",
        "",
        f"public class {HARNESS_CLASS} {{",
        f"    private static final String CORRECT_PREFIX = \"{CORRECT_PREFIX.replace('/', '.')}.\";",
        "",
        "    // Class of the thrown exception, with the relocation prefix of the correct version removed.",
        "    private static String kind(Throwable t) {",
        "        if (t == null) return \"(none)\";",
        "        String id = CProver.classIdentifier(t);",
        "        if (id.startsWith(CORRECT_PREFIX)) return id.substring(CORRECT_PREFIX.length());",
        "        return id;",
        "    }",
        "",
    ]
    lines = header + _helpers(method.return_type)
    if _helpers(method.return_type):
        lines.append("")
    lines.append(f"    public static void {ENTRY_METHOD}({', '.join(params)}) {{")
    lines += body
    lines.append(f"        assert kind(ec).equals(kind(ef)) : \"{EXCEPTION_ASSERT_MARK}\";")
    exception_line = len(lines)
    return_line = 0
    if ret != "void":
        lines.append(f"        assert ec != null || {_equal_expr(ret, 'rc', 'rf')} : \"{RETURN_ASSERT_MARK}\";")
        return_line = len(lines)
    lines += ["    }", "}", ""]
    return Harness(source="\n".join(lines), exception_line=exception_line, return_line=return_line)


REPLAY_CLASS = "VerifyReplay"
REPLAY_RESULT_PREFIX = "verify-replay-result="


def generate_replay(method: JavaMethod, values: Dict[str, Any]) -> str:
    """Plain Java program that runs both versions on one concrete input and prints the comparison.

    It mirrors the JBMC harness, but runs on the real JVM and JDK (no models or stubs), so a
    difference it reports is a real one.
    """
    ret = _strip(method.return_type)
    correct_cls = CORRECT_PREFIX.replace("/", ".") + "." + _source_class_name(method.class_name)
    fixed_cls = _source_class_name(method.class_name)
    lines = [
        "// Generated by defects4j_tools.verify_harness. Do not edit.",
        f"// replay of a JBMC counterexample for {method.display}",
        f"public class {REPLAY_CLASS} {{",
        f"    private static final String CORRECT_PREFIX = \"{CORRECT_PREFIX.replace('/', '.')}.\";",
        "    private static String kind(Throwable t) {",
        "        if (t == null) return \"(none)\";",
        "        String id = t.getClass().getName();",
        "        if (id.startsWith(CORRECT_PREFIX)) return id.substring(CORRECT_PREFIX.length());",
        "        return id;",
        "    }",
        "    private static String show(Object o) {",
        "        if (o instanceof Object[]) return java.util.Arrays.deepToString((Object[]) o);",
        "        if (o instanceof int[]) return java.util.Arrays.toString((int[]) o);",
        "        if (o instanceof long[]) return java.util.Arrays.toString((long[]) o);",
        "        if (o instanceof double[]) return java.util.Arrays.toString((double[]) o);",
        "        if (o instanceof float[]) return java.util.Arrays.toString((float[]) o);",
        "        if (o instanceof boolean[]) return java.util.Arrays.toString((boolean[]) o);",
        "        if (o instanceof char[]) return java.util.Arrays.toString((char[]) o);",
        "        if (o instanceof short[]) return java.util.Arrays.toString((short[]) o);",
        "        if (o instanceof byte[]) return java.util.Arrays.toString((byte[]) o);",
        "        return String.valueOf(o);",
        "    }",
    ]
    lines += _helpers(method.return_type)
    lines.append("    public static void main(String[] args) {")
    call_args_c, call_args_f = [], []
    for i, t in enumerate(method.param_types):
        t = _strip(t)
        lit = java_literal(t, values[f"p{i}"])
        if t.endswith("[]"):
            lines.append(f"        {t} p{i}_c = {lit};")
            lines.append(f"        {t} p{i}_f = {lit};")
            call_args_c.append(f"p{i}_c")
            call_args_f.append(f"p{i}_f")
        else:
            lines.append(f"        {t} p{i} = {lit};")
            call_args_c.append(f"p{i}")
            call_args_f.append(f"p{i}")
    call_c = f"{correct_cls}.{method.name}({', '.join(call_args_c)})"
    call_f = f"{fixed_cls}.{method.name}({', '.join(call_args_f)})"
    lines.append("        Throwable ec = null;")
    lines.append("        Throwable ef = null;")
    if ret != "void":
        default = "null" if ret.endswith("[]") or ret not in (_INTEGRAL | _FLOATING) else (
            "false" if ret == "boolean" else "0")
        lines.append(f"        {ret} rc = {default};")
        lines.append(f"        {ret} rf = {default};")
        lines.append(f"        try {{ rc = {call_c}; }} catch (Throwable t) {{ ec = t; }}")
        lines.append(f"        try {{ rf = {call_f}; }} catch (Throwable t) {{ ef = t; }}")
        same_return = _equal_expr(ret, "rc", "rf")
    else:
        lines.append(f"        try {{ {call_c}; }} catch (Throwable t) {{ ec = t; }}")
        lines.append(f"        try {{ {call_f}; }} catch (Throwable t) {{ ef = t; }}")
        same_return = "true"
    lines += [
        "        String result;",
        "        if (!kind(ec).equals(kind(ef))) result = \"exception_mismatch\";",
        f"        else if (ec == null && !({same_return})) result = \"return_mismatch\";",
        "        else result = \"same\";",
        "        System.out.println(\"correct_exception=\" + kind(ec));",
        "        System.out.println(\"fixed_exception=\" + kind(ef));",
    ]
    if ret != "void":
        lines += [
            "        System.out.println(\"correct_return=\" + show(rc));",
            "        System.out.println(\"fixed_return=\" + show(rf));",
        ]
    lines += [
        f"        System.out.println(\"{REPLAY_RESULT_PREFIX}\" + result);",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)
