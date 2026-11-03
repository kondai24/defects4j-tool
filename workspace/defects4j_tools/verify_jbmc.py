"""Run JBMC on a generated harness and read its JSON output.

JBMC is always run with ``--stop-on-fail``: finding one violated property is fast, while checking
every property one by one is often far slower than the time limit. Two kinds of run are used:

- ``full``   : harness assertions + unwinding assertions (``--unwinding-assertions``).
              No failure means: no difference within the bounds *and* the loop bound was sufficient.
- ``bounded``: harness assertions only, paths beyond the loop bound are cut off
              (``--no-unwinding-assertions``). Used after an unwinding failure to still look for a
              difference within the bound.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .verify_harness import ENTRY_METHOD, HARNESS_CLASS

JBMC_BIN = os.environ.get("JBMC_BIN", "jbmc")
CORE_MODELS = os.environ.get("JBMC_CORE_MODELS", "/usr/lib/core-models.jar")
CPROVER_API = os.environ.get("JBMC_CPROVER_API", str(Path(CORE_MODELS).with_name("cprover-api.jar")))

TIMEOUT_EXIT_CODES = {124, 137, -9}  # GNU timeout: 124 on SIGTERM, 128+9 after --kill-after; -9: killed


@dataclass
class JbmcOptions:
    # Loop bound: start small and double up to max_unwind (iterative deepening). 8 covers loops over
    # the bounded inputs below; 64 covers loops bounded by the bit width of int (e.g. binary gcd).
    unwind: int = 8
    max_unwind: int = 64
    max_string_length: int = 6
    max_array_length: int = 4
    memory_limit_mb: int = 4096  # per JBMC process (the container has 6 GiB)

    def unwind_schedule(self) -> List[int]:
        levels = [self.unwind]
        while levels[-1] * 2 <= self.max_unwind:
            levels.append(levels[-1] * 2)
        if levels[-1] < self.max_unwind:
            levels.append(self.max_unwind)
        return levels

    def as_dict(self) -> Dict[str, Any]:
        return {"unwind_schedule": self.unwind_schedule(), "max_nondet_string_length": self.max_string_length,
                "max_nondet_array_length": self.max_array_length, "memory_limit_mb": self.memory_limit_mb}


@dataclass
class JbmcRun:
    """Result of one JBMC run."""

    kind: str  # full | bounded
    outcome: str  # pass | harness_violation | unwind_violation | other_violation | timeout | memory | error
    elapsed_sec: float
    exit_code: Optional[int]
    log: str
    unwind: int = 0
    violated: Optional[Dict[str, Any]] = None  # {property, description, file, line, function}
    assertion: str = ""  # exception | return (for harness_violation)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {"kind": self.kind, "unwind": self.unwind, "outcome": self.outcome, "elapsed_sec": round(self.elapsed_sec, 2),
                "exit_code": self.exit_code, "log": self.log, "violated": self.violated,
                "assertion": self.assertion or None, "errors": self.errors[:5]}


def jbmc_version() -> str:
    try:
        out = subprocess.run([JBMC_BIN, "--version"], capture_output=True, text=True, check=False, timeout=60)
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"


def build_command(classpath: Sequence[str], options: JbmcOptions, kind: str, unwind: int) -> List[str]:
    if kind not in ("full", "bounded"):
        raise ValueError(kind)
    return [
        JBMC_BIN, HARNESS_CLASS,
        "--classpath", ":".join([CORE_MODELS, CPROVER_API, *classpath]),
        "--function", f"{HARNESS_CLASS}.{ENTRY_METHOD}",
        "--unwind", str(unwind),
        "--unwinding-assertions" if kind == "full" else "--no-unwinding-assertions",
        "--max-nondet-string-length", str(options.max_string_length),
        "--max-nondet-array-length", str(options.max_array_length),
        "--throw-runtime-exceptions",
        "--stop-on-fail",
        "--json-ui",
        "--trace",
    ]


def run(classpath: Sequence[str], options: JbmcOptions, kind: str, unwind: int, timeout_sec: float,
        log_path: Path, exception_line: int, return_line: int) -> JbmcRun:
    """Run JBMC with wall-clock and memory limits; the raw JSON output is saved to log_path."""
    if timeout_sec < 1:
        return JbmcRun(kind, "timeout", 0.0, None, str(log_path), unwind, errors=["no time left for this bug"])
    start = time.monotonic()
    jbmc = " ".join(_quote(a) for a in build_command(classpath, options, kind, unwind))
    # ulimit makes JBMC fail on its own allocation instead of the container's OOM killer
    # picking an arbitrary process (possibly the pipeline itself).
    script = f"ulimit -v {options.memory_limit_mb * 1024}; exec timeout --kill-after=5s {int(timeout_sec)}s {jbmc}"
    with log_path.open("w", encoding="utf-8") as out:
        proc = subprocess.run(["bash", "-c", script], stdout=out, stderr=subprocess.STDOUT, check=False)
    elapsed = time.monotonic() - start
    text = log_path.read_text(encoding="utf-8", errors="replace")
    result = parse_output(kind, proc.returncode, text, exception_line, return_line)
    if result.outcome == "timeout" and elapsed < timeout_sec - 1:
        result.outcome = "memory"  # killed before the time limit
    result.elapsed_sec = elapsed
    result.log = str(log_path)
    result.unwind = unwind
    return result


def _quote(arg: str) -> str:
    import shlex

    return shlex.quote(arg)


def parse_output(kind: str, exit_code: int, text: str, exception_line: int, return_line: int) -> JbmcRun:
    if exit_code in TIMEOUT_EXIT_CODES:
        return JbmcRun(kind, "timeout", 0.0, exit_code, "")
    try:
        data = json.loads(text)
    except ValueError:
        if "std::bad_alloc" in text or "Out of memory" in text or "out of memory" in text:
            return JbmcRun(kind, "memory", 0.0, exit_code, "", errors=[text[-500:]])
        return JbmcRun(kind, "error", 0.0, exit_code, "", errors=[text[-2000:]])
    if not isinstance(data, list):
        return JbmcRun(kind, "error", 0.0, exit_code, "", errors=["unexpected JSON"])
    errors = [m.get("messageText", "") for m in data if isinstance(m, dict) and m.get("messageType") == "ERROR"]
    status = next((m.get("cProverStatus") for m in data if isinstance(m, dict) and "cProverStatus" in m), None)

    violations: List[Dict[str, Any]] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        if "property" in m and str(m.get("status", "")).lower() in ("failed", "failure"):
            violations.append(m)
        for r in m.get("result", []) if isinstance(m.get("result"), list) else []:
            if str(r.get("status", "")).lower() in ("failed", "failure"):
                violations.append(r)

    if not violations:
        if exit_code == 0 and status == "success":
            return JbmcRun(kind, "pass", 0.0, exit_code, "", errors=errors)
        if any("memory" in e.lower() or "bad_alloc" in e for e in errors):
            return JbmcRun(kind, "memory", 0.0, exit_code, "", errors=errors)
        return JbmcRun(kind, "error", 0.0, exit_code, "", errors=errors or [f"exit code {exit_code}"])

    v = violations[0]
    loc = v.get("sourceLocation") or {}
    trace = v.get("trace") or []
    if not loc and trace:  # --stop-on-fail output: the location is on the last trace step
        loc = (trace[-1].get("sourceLocation") or {})
    desc = v.get("description", "")
    violated = {"property": v.get("property"), "description": desc, "file": loc.get("file"),
                "line": loc.get("line"), "function": loc.get("function")}
    in_harness = (loc.get("file") or "").endswith(f"{HARNESS_CLASS}.java")
    line = str(loc.get("line", ""))
    if in_harness and line == str(exception_line):
        return JbmcRun(kind, "harness_violation", 0.0, exit_code, "", violated=violated, assertion="exception",
                       trace=trace, errors=errors)
    if in_harness and return_line and line == str(return_line):
        return JbmcRun(kind, "harness_violation", 0.0, exit_code, "", violated=violated, assertion="return",
                       trace=trace, errors=errors)
    if "unwinding assertion" in desc:
        return JbmcRun(kind, "unwind_violation", 0.0, exit_code, "", violated=violated, errors=errors)
    return JbmcRun(kind, "other_violation", 0.0, exit_code, "", violated=violated, errors=errors)


# ---------------------------------------------------------------------------
# Counterexample decoding
# ---------------------------------------------------------------------------

class DecodeError(ValueError):
    pass


def _scalar(value: Dict[str, Any]) -> Any:
    data = value.get("data")
    vtype = str(value.get("type", ""))
    if "char" in vtype and value.get("binary"):
        return chr(int(value["binary"], 2))
    if "boolean" in vtype or data in ("true", "false"):
        return data == "true"
    if value.get("name") == "integer":
        if value.get("binary") and vtype.strip() in ("int", "const int"):
            n = int(value["binary"], 2)
            return n - (1 << 32) if n >= 1 << 31 else n
        try:
            return int(data)
        except (TypeError, ValueError):
            raise DecodeError(f"cannot read integer {data!r}")
    raise DecodeError(f"unsupported value {value!r}")


def decode_counterexample(trace: List[Dict[str, Any]], param_names: Sequence[str]) -> Dict[str, Any]:
    """Return {param: value} for the harness entry parameters (Strings and arrays included).

    Values are taken from the last assignments before the first call into the correct version.
    Raises DecodeError when a value cannot be reconstructed.
    """
    params: Dict[str, Dict[str, Any]] = {}
    objects: Dict[str, Dict[str, Any]] = {}
    members: Dict[str, Dict[str, Dict[str, Any]]] = {}
    entry = f"{HARNESS_CLASS}.{ENTRY_METHOD}"
    last_infinite_array = ""
    for step in trace:
        function = (step.get("sourceLocation") or {}).get("function", "") or \
            ((step.get("function") or {}).get("identifier", ""))
        if "jbmcverify.correct." in function:
            break
        if step.get("stepType") != "assignment" or "lhs" not in step:
            continue
        lhs, value = step["lhs"], step.get("value") or {}
        if lhs == "nondet_infinite_array_pointer" and value.get("name") == "pointer":
            # The characters of a nondet String are written to this array first; the String's
            # `data` member is then shown as "unknown", so remember the array to link them.
            last_infinite_array = str(value.get("data", "")).lstrip("&")
            continue
        if lhs in param_names and entry in function:
            params[lhs] = value
        elif "." in lhs:
            base, member = lhs.split(".", 1)
            field_name = member.split(".")[-1]
            if field_name == "data" and value.get("name") == "unknown" and last_infinite_array:
                value = {"name": "pointer", "data": last_infinite_array}
            members.setdefault(base, {})[field_name] = value
        else:
            objects[lhs] = value

    def member(name: str, field_name: str) -> Optional[Dict[str, Any]]:
        if field_name in members.get(name, {}):
            return members[name][field_name]

        def search(value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            for m in value.get("members", []) or []:
                if m.get("name") == field_name:
                    return m.get("value")
                found = search(m.get("value") or {})
                if found is not None:
                    return found
            return None

        return search(objects.get(name) or {})

    def decode(value: Dict[str, Any], depth: int = 0) -> Any:
        if depth > 3:
            raise DecodeError("nested too deeply")
        if value.get("name") != "pointer":
            return _scalar(value)
        target = str(value.get("data", ""))
        if target in ("null", "NULL", "0", ""):
            return None
        target = target.lstrip("&")
        length = member(target, "length")
        data = member(target, "data")
        if length is None or data is None:
            raise DecodeError(f"object {target} not found in the trace")
        n = _scalar(length)
        array_name = str(data.get("data", "")).lstrip("&")
        if not isinstance(n, int) or n < 0:
            raise DecodeError(f"bad length {n!r}")
        if n == 0:
            items: List[Any] = []
        else:
            arr = objects.get(array_name)
            if arr is None:
                raise DecodeError(f"array {array_name} not found in the trace")
            elems = {e.get("index"): e.get("value") for e in arr.get("elements", []) or []}
            if any(i not in elems for i in range(n)):
                raise DecodeError(f"array {array_name} is incomplete")
            items = [decode(elems[i], depth + 1) for i in range(n)]
        if "java.lang.String" in str(value.get("type", "")) and "array" not in str(value.get("type", "")):
            return "".join(items)
        return items

    decoded: Dict[str, Any] = {}
    for name in param_names:
        if name not in params:
            raise DecodeError(f"parameter {name} not found in the trace")
        decoded[name] = decode(params[name])
    return decoded
