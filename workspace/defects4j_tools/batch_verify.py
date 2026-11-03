#!/usr/bin/env python3
"""verify step: detect overfitting patches with JBMC.

For every row of <target_dir>/results.csv, the developer-fixed (correct) version and the LLM-fixed
version of each method the developer changed are run on the same arbitrary inputs by JBMC. The row
is written as one of:

- success       : no difference within the bounds, and the loop bound was sufficient
- fail          : a different return value or a different exception class was found
- unverifiable  : could not be checked; ``verify_reason`` says why

Details per bug go to <target_dir>/verify/<Project>-<id>/result.json.
The correct version is checked out once into <correct_dir>/<Project>-<id> (shared by all variants)
and never modified; it is copied to a work directory before compiling.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import verify_harness, verify_jbmc, verify_targets
from .verify_jbmc import JbmcOptions
from .verify_targets import JavaMethod

STATUS_COL = "verify_status"
REASON_COL = "verify_reason"
TIME_COL = "verify_time_sec"
VERIFY_COLUMNS = [STATUS_COL, REASON_COL, TIME_COL]

DEFAULT_TIMEOUT_SEC = 300
DEFECTS4J_HOME = Path(os.environ.get("DEFECTS4J_HOME", "/defects4j"))
REWRITER_SOURCE = Path(__file__).resolve().parent / "verify_support" / "ClassRewriter.java"
ASM_EXPORTS = [
    "--add-exports", "java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED",
    "--add-exports", "java.base/jdk.internal.org.objectweb.asm.commons=ALL-UNNAMED",
]
COMPLETE_SUFFIX = ".complete"
MAX_SPURIOUS_RETRIES = 3  # re-runs of JBMC after excluding inputs that did not reproduce on the JVM
REPLAY_TIMEOUT_SEC = 60

# When several methods give different "unverifiable" reasons, the row gets the first one here.
REASON_PRIORITY = [
    "not_found", "compile_error", "checkout_error", "patch_error", "no_target_method",
    "unsupported_signature", "target_missing", "correct_compile_error", "harness_error",
    "jbmc_error", "unexpected_property", "memory_limit", "timeout", "unwind_limit", "spurious_counterexample",
    "unconfirmed_counterexample",
]


class StepError(Exception):
    """A failure that makes the whole row unverifiable with the given reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass
class VerifyConfig:
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    jbmc: JbmcOptions = field(default_factory=JbmcOptions)
    correct_dir: Optional[str] = None  # default: <target_dir>/../correct
    keep_work: bool = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _run(cmd: Sequence[str], cwd: Optional[Path] = None, log: Optional[Path] = None,
         timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          check=False, timeout=timeout)
    if log is not None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(cmd)}  (cwd={cwd})\n{proc.stdout}{proc.stderr}\n[exit {proc.returncode}]\n\n")
    return proc


def _d4j_export(checkout: Path, prop: str, log: Path) -> str:
    proc = _run(["defects4j", "export", "-p", prop], cwd=checkout, log=log)
    if proc.returncode != 0:
        raise StepError("compile_error", f"defects4j export -p {prop} failed")
    return proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""


def _abs(checkout: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else checkout / p


def _class_internal_names(classes_dir: Path) -> List[str]:
    return sorted(str(p.relative_to(classes_dir).with_suffix("")).replace(os.sep, "/")
                  for p in classes_dir.rglob("*.class"))


def _outer_chain(binary_name: str) -> List[str]:
    """org.a.Outer$In$Deep -> [org/a/Outer, org/a/Outer$In, org/a/Outer$In$Deep]."""
    internal = binary_name.replace(".", "/")
    parts = internal.split("$")
    return ["$".join(parts[: i + 1]) for i in range(len(parts))]


def _sanitize(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:80]


# ---------------------------------------------------------------------------
# Correct version cache (decision 9)
# ---------------------------------------------------------------------------

def correct_marker(correct_root: Path, project: str, bug_id: str) -> Path:
    return correct_root / f"{project}-{bug_id}{COMPLETE_SUFFIX}"


def ensure_correct_checkout(correct_root: Path, project: str, bug_id: str, log: Path) -> Path:
    """Check out <project>-<id>f only when no completed checkout exists.

    Completion is recorded in a marker file next to the checkout (not inside it). A directory without
    the marker is a checkout that stopped midway, so it is removed and checked out again.
    """
    checkout = correct_root / f"{project}-{bug_id}"
    marker = correct_marker(correct_root, project, bug_id)
    if marker.is_file() and checkout.is_dir():
        return checkout
    correct_root.mkdir(parents=True, exist_ok=True)
    if checkout.is_symlink() or checkout.is_file():
        checkout.unlink()
    elif checkout.exists():
        shutil.rmtree(checkout)
    marker.unlink(missing_ok=True)
    proc = _run(["defects4j", "checkout", "-p", project, "-v", f"{bug_id}f", "-w", str(checkout)], log=log)
    if proc.returncode != 0 or not checkout.is_dir():
        raise StepError("checkout_error", f"defects4j checkout {project}-{bug_id}f failed")
    marker.write_text(json.dumps({
        "project": project, "id": bug_id, "version": f"{bug_id}f",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    return checkout


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------

@dataclass
class MethodResult:
    method: str
    return_type: str
    status: str
    reason: str = ""
    detail: str = ""
    descriptor: Optional[str] = None
    elapsed_sec: Optional[float] = None
    counterexample: Optional[Dict[str, Any]] = None
    harness: Optional[str] = None
    unwind: Optional[int] = None  # loop bound of the last JBMC run (the bound proven sufficient on success)
    attempts: List[Dict[str, Any]] = field(default_factory=list)  # each JBMC run (with its log file)
    spurious_counterexamples: List[Dict[str, Any]] = field(default_factory=list)


def _aggregate(results: List[MethodResult]) -> Tuple[str, str]:
    fails = [r for r in results if r.status == "fail"]
    if fails:
        return "fail", fails[0].reason
    reasons = [r.reason for r in results if r.status == "unverifiable"]
    if reasons:
        reasons.sort(key=lambda x: REASON_PRIORITY.index(x) if x in REASON_PRIORITY else len(REASON_PRIORITY))
        return "unverifiable", reasons[0]
    if not results:
        return "unverifiable", "no_target_method"
    return "success", ""


def _compile(checkout: Path, log: Path, reason: str) -> None:
    proc = _run(["defects4j", "compile"], cwd=checkout, log=log)
    if proc.returncode != 0 or "BUILD FAILED" in proc.stdout + proc.stderr:
        raise StepError(reason, f"defects4j compile failed in {checkout}")


def _llm_changes(correct_src_root: Path, fixed_src_root: Path, buggy_sources: Dict[str, str],
                 src_rel: str, targets: List[JavaMethod]) -> Dict[str, Any]:
    """Which methods did the LLM change, compared with the (reconstructed) buggy version?"""
    changed_files: List[str] = []
    changed: List[str] = []
    outside_methods = False
    target_keys = {m.key for m in targets}
    other_methods: List[str] = []
    fixed_files = {str(p.relative_to(fixed_src_root)) for p in fixed_src_root.rglob("*.java")}
    correct_files = {str(p.relative_to(correct_src_root)) for p in correct_src_root.rglob("*.java")}
    for rel in sorted(fixed_files | correct_files):
        path_in_checkout = f"{src_rel}/{rel}" if src_rel else rel
        if path_in_checkout in buggy_sources:
            before: Optional[str] = buggy_sources[path_in_checkout]
        elif rel in correct_files:
            before = (correct_src_root / rel).read_text(encoding="utf-8", errors="replace")
        else:
            before = None
        after = (fixed_src_root / rel).read_text(encoding="utf-8", errors="replace") if rel in fixed_files else None
        if before == after:
            continue
        changed_files.append(path_in_checkout)
        if before is None or after is None:
            outside_methods = True
            continue
        methods, outside = verify_targets.changed_methods(before, after)
        outside_methods = outside_methods or outside
        for m in methods:
            changed.append(m.display)
            if m.key not in target_keys:
                other_methods.append(m.display)
    return {
        "changed_files": changed_files,
        "changed_methods": changed,
        "changed_non_target_methods": other_methods,
        "changed_outside_methods": outside_methods,
        "changed_outside_target": bool(other_methods) or outside_methods,
    }


def verify_row(target_dir: Path, project: str, bug_id: str, config: VerifyConfig,
               correct_root: Path) -> Tuple[str, str, float, Dict[str, Any]]:
    """Verify one bug; returns (status, reason, elapsed seconds, result.json payload)."""
    start = time.monotonic()
    bug = f"{project}-{bug_id}"
    out_dir = target_dir / "verify" / bug
    if out_dir.exists():
        shutil.rmtree(out_dir)
    logs = out_dir / "logs"
    logs.mkdir(parents=True)
    work = out_dir / ".work"
    work.mkdir()
    log = logs / "commands.log"
    fixed_checkout = target_dir / bug
    record: Dict[str, Any] = {
        "project": project,
        "id": bug_id,
        "fixed_checkout": str(fixed_checkout),
        "correct_checkout": str(correct_root / bug),
        "patch": str(DEFECTS4J_HOME / "framework/projects" / project / "patches" / f"{bug_id}.src.patch"),
        "jbmc": {"version": verify_jbmc.jbmc_version(), **config.jbmc.as_dict(),
                 "timeout_sec_per_bug": config.timeout_sec},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "logs": str(logs),
    }
    methods: List[MethodResult] = []
    try:
        if not fixed_checkout.is_dir():
            raise StepError("not_found", f"{fixed_checkout} does not exist")

        # 1. The LLM-fixed version must compile (decision 8). It is compiled in place, as `test` does.
        _compile(fixed_checkout, log, "compile_error")

        # 2. Correct version (decision 9): check out only when missing.
        correct_checkout = ensure_correct_checkout(correct_root, project, bug_id, log)

        # 3. Target methods from src.patch (decisions 5 and 10).
        patch_path = Path(record["patch"])
        try:
            patch = verify_targets.parse_unified_diff(patch_path.read_text(encoding="utf-8", errors="replace"))
            correct_sources = {p: (correct_checkout / p).read_text(encoding="utf-8", errors="replace")
                               for p in patch if p.endswith(".java")}
            report, buggy_sources = verify_targets.find_targets(correct_sources, patch)
        except (OSError, ValueError) as exc:
            raise StepError("patch_error", str(exc))
        record["targets"] = [m.display for m in report.targets]
        record["developer_added_methods"] = report.added_by_developer
        record["developer_removed_methods"] = report.removed_by_developer
        record["developer_changes_outside_methods"] = report.outside_methods
        if not report.targets:
            raise StepError("no_target_method", "the developer patch changes no method that exists in both versions")

        src_rel = _d4j_export(fixed_checkout, "dir.src.classes", log)
        record["llm_changes"] = _llm_changes(correct_checkout / src_rel, fixed_checkout / src_rel,
                                             buggy_sources, src_rel, report.targets)

        # 4. Signature check (decision 6) and presence in the LLM-fixed version.
        fixed_methods: Dict[str, Dict[Tuple, JavaMethod]] = {}
        to_check: List[Tuple[JavaMethod, MethodResult]] = []
        for m in report.targets:
            res = MethodResult(method=m.display, return_type=m.return_type, status="pending",
                               descriptor=verify_targets.method_descriptor(m))
            methods.append(res)
            why = verify_targets.unsupported_reason(m)
            if why:
                res.status, res.reason, res.detail = "unverifiable", "unsupported_signature", why
                continue
            path = next(p for p in correct_sources if _declares(correct_sources[p], m))
            if path not in fixed_methods:
                fixed_file = fixed_checkout / path
                text = fixed_file.read_text(encoding="utf-8", errors="replace") if fixed_file.is_file() else ""
                fixed_methods[path] = {fm.key: fm for fm in verify_targets.extract_methods(text)}
            fm = fixed_methods[path].get(m.key)
            if fm is None or not fm.is_static or fm.return_type != m.return_type:
                res.status, res.reason = "unverifiable", "target_missing"
                res.detail = "method not found in the LLM-fixed version with the same static signature"
                continue
            to_check.append((m, res))

        if to_check:
            _check_methods(to_check, fixed_checkout, correct_checkout, work, out_dir, logs, log, config,
                           budget_start=time.monotonic())
        status, reason = _aggregate(methods)
    except StepError as exc:
        status, reason = "unverifiable", exc.reason
        record["error"] = exc.detail or exc.reason
    except Exception as exc:  # unexpected: keep the batch going, but make it visible
        status, reason = "unverifiable", "internal_error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if not config.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    elapsed = time.monotonic() - start
    record.update({
        "verify_status": status,
        "verify_reason": reason,
        "verify_time_sec": round(elapsed, 2),
        "methods": [asdict(m) for m in methods],
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    (out_dir / "result.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return status, reason, elapsed, record


def _declares(source: str, method: JavaMethod) -> bool:
    return any(m.key == method.key for m in verify_targets.extract_methods(source))


def _check_methods(to_check: List[Tuple[JavaMethod, MethodResult]], fixed_checkout: Path, correct_checkout: Path,
                   work: Path, out_dir: Path, logs: Path, log: Path, config: VerifyConfig,
                   budget_start: float) -> None:
    def mark_all(reason: str, detail: str) -> None:
        for _, res in to_check:
            res.status, res.reason, res.detail = "unverifiable", reason, detail

    # Compile a copy of the correct version (the cached checkout stays untouched).
    correct_work = work / "correct"
    shutil.copytree(correct_checkout, correct_work, symlinks=True)
    try:
        _compile(correct_work, log, "correct_compile_error")
    except StepError as exc:
        mark_all(exc.reason, exc.detail)
        return
    correct_classes = _abs(correct_work, _d4j_export(correct_work, "dir.bin.classes", log))
    fixed_classes = _abs(fixed_checkout, _d4j_export(fixed_checkout, "dir.bin.classes", log))
    cp_compile = _d4j_export(fixed_checkout, "cp.compile", log)
    deps = [e for e in cp_compile.split(":") if e and Path(e).resolve() != fixed_classes.resolve()]

    # Relocate every class of the correct version under CORRECT_PREFIX so both versions can be loaded
    # together, and make each target method (and its classes) public for the harness.
    publics: List[str] = []
    copies: Set[str] = set()
    for m, res in to_check:
        for name in _outer_chain(m.class_name):
            publics.append(f"public {name}")
            copies.add(name)
        publics.append(f"public-method {m.class_name.replace('.', '/')} {m.name} {res.descriptor}")
    relocated = work / "correct-relocated"
    overlay = work / "fixed-overlay"
    spec_c = work / "correct.spec"
    spec_f = work / "fixed.spec"
    spec_c.write_text("\n".join([f"rename {n}" for n in _class_internal_names(correct_classes)] + publics) + "\n")
    spec_f.write_text("\n".join([f"copy {n}" for n in sorted(copies)] + publics) + "\n")
    for classes, out, prefix, spec in ((correct_classes, relocated, verify_harness.CORRECT_PREFIX, spec_c),
                                       (fixed_classes, overlay, "-", spec_f)):
        proc = _run(["java", *ASM_EXPORTS, str(REWRITER_SOURCE), str(classes), str(out), prefix, str(spec)], log=log)
        if proc.returncode != 0:
            mark_all("harness_error", "class rewriting failed")
            return

    classpath_tail = [str(relocated), str(overlay), str(fixed_classes), *deps]
    for idx, (m, res) in enumerate(to_check, 1):
        name = f"{idx:02d}-{_sanitize(m.name)}"
        method_start = time.monotonic()
        # The time limit is per bug; each remaining method gets an equal share of what is left.
        left = config.timeout_sec - (method_start - budget_start)
        deadline = method_start + left / (len(to_check) - idx + 1)
        _check_one(m, res, name, classpath_tail, work, out_dir, logs, log, config, deadline)
        res.elapsed_sec = round(time.monotonic() - method_start, 2)


def _compile_java(source: Path, classes: Path, classpath: Sequence[str], log: Path) -> bool:
    classes.mkdir(parents=True, exist_ok=True)
    proc = _run(["javac", "-nowarn", "-g", "-source", "8", "-target", "8", "-cp", ":".join(classpath),
                 "-d", str(classes), str(source)], log=log)
    return proc.returncode == 0


def _replay(m: JavaMethod, values: Dict[str, Any], rdir: Path, classpath_tail: Sequence[str], work: Path,
            log: Path) -> Dict[str, Any]:
    """Run both versions on the JVM with the counterexample input; returns the parsed output."""
    rdir.mkdir(parents=True, exist_ok=True)
    src = rdir / f"{verify_harness.REPLAY_CLASS}.java"
    try:
        src.write_text(verify_harness.generate_replay(m, values), encoding="utf-8")
    except (ValueError, TypeError, KeyError) as exc:
        return {"result": "error", "error": f"cannot build replay: {exc}"}
    classes = work / "replay" / rdir.name
    if not _compile_java(src, classes, list(classpath_tail), log):
        return {"result": "error", "error": "javac failed for the replay", "source": str(src)}
    try:
        proc = _run(["java", "-cp", ":".join([str(classes), *classpath_tail]), verify_harness.REPLAY_CLASS],
                    log=log, timeout=REPLAY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return {"result": "error", "error": f"replay did not finish in {REPLAY_TIMEOUT_SEC}s", "source": str(src)}
    out: Dict[str, Any] = {"source": str(src)}
    for line in proc.stdout.splitlines():
        if line.startswith(verify_harness.REPLAY_RESULT_PREFIX):
            out["result"] = line[len(verify_harness.REPLAY_RESULT_PREFIX):].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    out.setdefault("result", "error")
    if out["result"] == "error":
        out["error"] = (proc.stdout + proc.stderr)[-1000:]
    return out


def _check_one(m: JavaMethod, res: MethodResult, name: str, classpath_tail: Sequence[str], work: Path,
               out_dir: Path, logs: Path, log: Path, config: VerifyConfig, deadline: float) -> None:
    """JBMC search + JVM confirmation for one method (decisions 4 and 7).

    The loop bound grows by iterative deepening (config.jbmc.unwind_schedule()). At each bound:

    - no violated property (harness or unwinding)            -> success
    - a difference that the JVM replay reproduces             -> fail
    - a difference that does not reproduce (spurious; usually
      JDK methods that JBMC does not model)                    -> exclude that input and retry
    - only the loop bound is violated                          -> look for a difference within the
                                                                  bound, then try the next bound
    The last bound still too small                             -> unverifiable (unwind_limit)
    """
    params = [f"p{i}" for i in range(len(m.param_types))]
    hdir = out_dir / "harness" / name
    exclusions: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    spurious: List[Dict[str, Any]] = []
    res.attempts = attempts
    res.spurious_counterexamples = spurious
    run_no = 0

    def finish(status: str, reason: str, detail: str = "") -> None:
        res.status, res.reason = status, reason
        if detail:
            res.detail = detail

    def build_harness() -> Optional[Tuple[verify_harness.Harness, List[str]]]:
        harness = verify_harness.generate(m, exclusions)
        sub = "" if not exclusions else f"exclude{len(exclusions)}/"
        hsrc = hdir / f"{sub}{verify_harness.HARNESS_CLASS}.java"
        hsrc.parent.mkdir(parents=True, exist_ok=True)
        hsrc.write_text(harness.source, encoding="utf-8")
        res.harness = str(hdir / f"{verify_harness.HARNESS_CLASS}.java")
        hclasses = work / "harness" / name / f"exclude{len(exclusions)}"
        if not _compile_java(hsrc, hclasses, [verify_jbmc.CPROVER_API, *classpath_tail], log):
            return None
        return harness, [str(hclasses), *classpath_tail]

    built = build_harness()
    if built is None:
        return finish("unverifiable", "harness_error", "javac failed for the harness")
    harness, cp = built

    def jbmc(kind: str, unwind: int) -> verify_jbmc.JbmcRun:
        nonlocal run_no
        run_no += 1
        run = verify_jbmc.run(cp, config.jbmc, kind, unwind, deadline - time.monotonic(),
                              logs / f"{name}.run{run_no:02d}.{kind}.unwind{unwind}.jbmc.json",
                              harness.exception_line, harness.return_line)
        attempts.append(run.summary())
        return run

    for unwind in config.jbmc.unwind_schedule():
        res.unwind = unwind
        while True:
            run = jbmc("full", unwind)
            if run.outcome == "unwind_violation":
                run = jbmc("bounded", unwind)  # still look for a difference within this bound
                if run.outcome == "pass":
                    break  # no difference within the bound: try a larger bound
            if run.outcome == "pass":
                return finish("success", "")
            if run.outcome == "timeout":
                return finish("unverifiable", "timeout")
            if run.outcome == "memory":
                return finish("unverifiable", "memory_limit", f"JBMC exceeded {config.jbmc.memory_limit_mb} MB")
            if run.outcome == "error":
                return finish("unverifiable", "jbmc_error", "; ".join(e for e in run.errors if e)[:2000])
            if run.outcome != "harness_violation":
                return finish("unverifiable", "unexpected_property", json.dumps(run.violated, ensure_ascii=False))

            # JBMC reports a difference: confirm it on the JVM.
            try:
                values = verify_jbmc.decode_counterexample(run.trace, params)
            except verify_jbmc.DecodeError as exc:
                return finish("unverifiable", "unconfirmed_counterexample", f"cannot read the counterexample: {exc}")
            replay = _replay(m, values, hdir / f"replay{len(spurious) + 1}", classpath_tail, work, log)
            entry = {"input": values, "jbmc_assertion": run.assertion, "unwind": unwind, "replay": replay}
            if replay.get("result") in ("exception_mismatch", "return_mismatch"):
                res.counterexample = entry
                return finish("fail", replay["result"])
            if replay.get("result") != "same":
                res.counterexample = entry
                return finish("unverifiable", "unconfirmed_counterexample", replay.get("error", ""))
            spurious.append(entry)
            if not verify_harness.can_exclude(m) or len(spurious) > MAX_SPURIOUS_RETRIES:
                return finish("unverifiable", "spurious_counterexample",
                              f"{len(spurious)} JBMC counterexample(s) did not reproduce on the JVM")
            exclusions.append(values)
            built = build_harness()
            if built is None:
                return finish("unverifiable", "harness_error", "javac failed for the harness")
            harness, cp = built
    return finish("unverifiable", "unwind_limit",
                  f"loop bound {config.jbmc.unwind_schedule()[-1]} was not sufficient")


# ---------------------------------------------------------------------------
# results.csv
# ---------------------------------------------------------------------------

def _read_rows(csv_file: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with csv_file.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
    for col in VERIFY_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    return fieldnames, rows


def _write_rows(csv_file: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp = csv_file.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    tmp.replace(csv_file)


def _selected(rows: List[Dict[str, str]], bugs: Optional[Iterable[str]]) -> List[Dict[str, str]]:
    wanted = {b.strip() for b in bugs or [] if b.strip()}
    picked = [r for r in rows if r.get("project") and r.get("id")]
    if wanted:
        picked = [r for r in picked if f"{r['project'].strip()}-{r['id'].strip()}" in wanted]
    return picked


def run(target_dir: str, config: Optional[VerifyConfig] = None, bugs: Optional[Iterable[str]] = None) -> int:
    """Verify rows of <target_dir>/results.csv (all rows, or only ``bugs`` like ["Math-99"])."""
    config = config or VerifyConfig()
    target = Path(target_dir).resolve()
    csv_file = target / "results.csv"
    if not target.is_dir():
        print(f"Error: target directory '{target_dir}' not found", file=sys.stderr)
        return 1
    if not csv_file.is_file():
        print(f"Error: {csv_file} not found", file=sys.stderr)
        return 1
    if shutil.which(verify_jbmc.JBMC_BIN) is None:
        print(f"Error: '{verify_jbmc.JBMC_BIN}' not found. Rebuild the Docker image (docker compose up -d --build).",
              file=sys.stderr)
        return 1
    correct_root = Path(config.correct_dir).resolve() if config.correct_dir else target.parent / "correct"

    fieldnames, rows = _read_rows(csv_file)
    selected = _selected(rows, bugs)
    if bugs and not selected:
        print(f"Error: none of {sorted(bugs)} is in {csv_file}", file=sys.stderr)
        return 1
    for row in selected:  # clear stale values first so an interrupted run never mixes old and new results
        for col in VERIFY_COLUMNS:
            row[col] = ""
    _write_rows(csv_file, fieldnames, rows)

    print(f"Found {len(selected)} rows to verify")
    print(f"Target directory: {target}")
    print(f"Correct versions: {correct_root}")
    print(f"JBMC: {verify_jbmc.jbmc_version()}  {config.jbmc.as_dict()}  timeout/bug={config.timeout_sec}s")
    print("-" * 60)
    counts: Dict[str, int] = {}
    for i, row in enumerate(selected, 1):
        project, bug_id = row["project"].strip(), row["id"].strip()
        print(f"\n[{i}/{len(selected)}] Verifying {project}-{bug_id}")
        status, reason, elapsed, record = verify_row(target, project, bug_id, config, correct_root)
        row[STATUS_COL], row[REASON_COL], row[TIME_COL] = status, reason, f"{elapsed:.2f}"
        _write_rows(csv_file, fieldnames, rows)
        counts[status] = counts.get(status, 0) + 1
        for m in record.get("methods", []):
            extra = f" ({m['reason']})" if m.get("reason") else ""
            cex = ""
            if m.get("counterexample"):
                cex = f" input={json.dumps(m['counterexample']['input'], ensure_ascii=False)}"
            print(f"    - {m['method']}: {m['status']}{extra}{cex}")
        mark = {"success": "✓", "fail": "✗"}.get(status, "?")
        print(f"{mark} {project}-{bug_id}: {status}" + (f" ({reason})" if reason else "") + f" [{elapsed:.1f}s]")

    print("\n" + "=" * 60)
    print("SUMMARY (verify)")
    print("=" * 60)
    print(f"Total rows: {len(selected)}")
    for key in ("success", "fail", "unverifiable"):
        print(f"{key}: {counts.get(key, 0)}")
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    args = parser.parse_args()
    sys.exit(run(args.target_dir, config_from_args(args), args.bugs))


def add_arguments(parser) -> None:
    parser.add_argument("--dir", dest="target_dir", required=True, help="Target directory (contains results.csv)")
    parser.add_argument("--bugs", type=lambda s: [x for x in s.split(",") if x.strip()], default=None,
                        help="Comma-separated bugs to verify, e.g. Math-99,Lang-41 (default: all rows)")
    parser.add_argument("--correct-dir", default=None, help="Cache of correct versions (default: <dir>/../correct)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="JBMC time limit per bug (sec)")
    parser.add_argument("--unwind", type=int, default=JbmcOptions.unwind, help="Initial loop bound N for JBMC")
    parser.add_argument("--max-unwind", type=int, default=JbmcOptions.max_unwind,
                        help="Largest loop bound (N is doubled up to this value)")
    parser.add_argument("--memory-limit-mb", type=int, default=JbmcOptions.memory_limit_mb)
    parser.add_argument("--max-string-length", type=int, default=JbmcOptions.max_string_length)
    parser.add_argument("--max-array-length", type=int, default=JbmcOptions.max_array_length)
    parser.add_argument("--keep-work", action="store_true", help="Keep compiled work files for debugging")


def config_from_args(args) -> VerifyConfig:
    return VerifyConfig(
        timeout_sec=args.timeout,
        jbmc=JbmcOptions(unwind=args.unwind, max_unwind=args.max_unwind, max_string_length=args.max_string_length,
                         max_array_length=args.max_array_length, memory_limit_mb=args.memory_limit_mb),
        correct_dir=args.correct_dir,
        keep_work=args.keep_work,
    )


def config_from_plan(value: Any) -> VerifyConfig:
    """Build a config from the optional ``verify:`` mapping of a plan (defaults or variant)."""
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("verify must be a mapping")
    known = {"timeout", "unwind", "max_unwind", "max_string_length", "max_array_length", "memory_limit_mb",
             "correct_dir"}
    unknown = set(value) - known
    if unknown:
        raise ValueError(f"unknown verify keys: {sorted(unknown)}. Valid: {sorted(known)}")
    defaults = JbmcOptions()
    return VerifyConfig(
        timeout_sec=int(value.get("timeout", DEFAULT_TIMEOUT_SEC)),
        jbmc=JbmcOptions(unwind=int(value.get("unwind", defaults.unwind)),
                         max_unwind=int(value.get("max_unwind", defaults.max_unwind)),
                         memory_limit_mb=int(value.get("memory_limit_mb", defaults.memory_limit_mb)),
                         max_string_length=int(value.get("max_string_length", defaults.max_string_length)),
                         max_array_length=int(value.get("max_array_length", defaults.max_array_length))),
        correct_dir=value.get("correct_dir"),
    )


if __name__ == "__main__":
    main()
