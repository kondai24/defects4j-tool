"""Tests for the verify step (JBMC-based overfitting patch detection).

Parts that need tree-sitter are skipped when tree_sitter_languages is not installed
(it is installed in the Docker image). JBMC itself is not needed: JBMC output and traces
are given as fixtures.
"""

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace"))
import pipeline  # noqa: E402
from defects4j_tools import batch_verify, verify_harness, verify_jbmc, verify_targets  # noqa: E402
from defects4j_tools.batch_verify import MethodResult  # noqa: E402
from defects4j_tools.verify_targets import JavaMethod  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:
    import tree_sitter_languages  # noqa: F401

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

CORRECT = """package p;

public class C {
    public static int gcd(int a, int b) {
        if (a == 0) {
            return Math.abs(b);
        }
        return gcd(b % a, a);
    }

    public static int untouched(int x) {
        return x;
    }

    public int size() {
        return 1;
    }
}
"""

# Defects4J patches go from the correct (fixed) version to the buggy version.
PATCH = """--- a/src/p/C.java
+++ b/src/p/C.java
@@ -4,7 +4,7 @@
     public static int gcd(int a, int b) {
         if (a == 0) {
-            return Math.abs(b);
+            return b;
         }
         return gcd(b % a, a);
     }
"""


def method(params, ret="int", name="m", static=True, ctor=False):
    return JavaMethod(class_name="p.C", name=name, param_types=tuple(params), return_type=ret, is_static=static,
                      is_constructor=ctor, start_line=1, end_line=2, in_anonymous_or_local=False)


class DiffTests(unittest.TestCase):
    def test_parse_and_apply_gives_the_buggy_source(self):
        patch_files = verify_targets.parse_unified_diff(PATCH)
        self.assertEqual(list(patch_files), ["src/p/C.java"])
        hunk = patch_files["src/p/C.java"][0]
        self.assertEqual(hunk.changed_lines(), ({6}, {6}))
        buggy = verify_targets.apply_hunks(CORRECT, patch_files["src/p/C.java"])
        self.assertIn("            return b;\n", buggy)
        self.assertNotIn("Math.abs", buggy)
        self.assertIn("untouched", buggy)

    def test_apply_rejects_a_patch_that_does_not_match(self):
        hunks = verify_targets.parse_unified_diff(PATCH)["src/p/C.java"]
        with self.assertRaises(ValueError):
            verify_targets.apply_hunks(CORRECT.replace("Math.abs(b)", "-b"), hunks)


@unittest.skipUnless(HAS_TREE_SITTER, "tree_sitter_languages is not installed")
class TargetTests(unittest.TestCase):
    def test_find_targets_returns_only_the_changed_method(self):
        report, buggy = verify_targets.find_targets({"src/p/C.java": CORRECT},
                                                    verify_targets.parse_unified_diff(PATCH))
        self.assertEqual([m.display for m in report.targets], ["p.C.gcd(int, int)"])
        target = report.targets[0]
        self.assertTrue(target.is_static)
        self.assertEqual(target.return_type, "int")
        self.assertEqual(report.added_by_developer, [])
        self.assertEqual(report.outside_methods, {})
        self.assertIn("return b;", buggy["src/p/C.java"])

    def test_extract_methods_reads_nested_classes_and_generics(self):
        src = """package q;
class Outer {
    static class Inner {
        static <T> java.util.List<String> f(final String[] xs, @Deprecated int n) { return null; }
    }
    Outer(int x) {}
}
"""
        methods = {m.name: m for m in verify_targets.extract_methods(src)}
        f = methods["f"]
        self.assertEqual(f.class_name, "q.Outer$Inner")
        self.assertEqual(f.param_types, ("String[]", "int"))
        self.assertEqual(f.return_type, "java.util.List")
        self.assertTrue(methods["<init>"].is_constructor)

    def test_changed_methods_reports_other_methods_and_outside_changes(self):
        after = CORRECT.replace("return x;", "return x + 1;").replace("public class C {",
                                                                      "public class C {\n    static int K = 1;")
        methods, outside = verify_targets.changed_methods(CORRECT, after)
        self.assertEqual([m.name for m in methods], ["untouched"])
        self.assertTrue(outside)
        methods, outside = verify_targets.changed_methods(CORRECT, CORRECT + "\n")
        self.assertEqual(methods, [])
        self.assertFalse(outside)


class SignatureTests(unittest.TestCase):
    def test_supported_signatures(self):
        self.assertIsNone(verify_targets.unsupported_reason(method(["int", "boolean", "String"], "long")))
        self.assertIsNone(verify_targets.unsupported_reason(method(["int[]", "String[]"], "double[]")))
        self.assertIsNone(verify_targets.unsupported_reason(method([], "void")))

    def test_unsupported_signatures(self):
        self.assertIn("instance", verify_targets.unsupported_reason(method(["int"], static=False)))
        self.assertIn("constructor", verify_targets.unsupported_reason(
            method(["int"], ret="void", name="<init>", static=False, ctor=True)))
        self.assertIn("parameter", verify_targets.unsupported_reason(method(["long"])))
        self.assertIn("parameter", verify_targets.unsupported_reason(method(["CharSequence", "char[]"])))
        self.assertIn("return", verify_targets.unsupported_reason(method(["String"], "Number")))

    def test_descriptor(self):
        m = method(["int", "String[]", "boolean"], "java.lang.Double")
        self.assertEqual(verify_targets.method_descriptor(m), "(I[Ljava/lang/String;Z)Ljava/lang/Double;")


class HarnessTests(unittest.TestCase):
    def test_assertion_lines_point_to_the_two_assertions(self):
        h = verify_harness.generate(method(["int", "String"], "double", name="f"))
        lines = h.source.splitlines()
        self.assertIn(verify_harness.EXCEPTION_ASSERT_MARK, lines[h.exception_line - 1])
        self.assertIn(verify_harness.RETURN_ASSERT_MARK, lines[h.return_line - 1])
        self.assertIn("jbmcverify.correct.p.C.f(p0, p1)", h.source)
        self.assertIn("p.C.f(p0, p1)", h.source)

    def test_void_method_has_no_return_assertion(self):
        h = verify_harness.generate(method(["int[]"], "void", name="g"))
        self.assertEqual(h.return_line, 0)
        self.assertNotIn(verify_harness.RETURN_ASSERT_MARK, h.source)
        # each version gets its own copy of the array
        self.assertIn("clone()", h.source)

    def test_exclusions_become_assumptions(self):
        m = method(["int", "String"], "int", name="f")
        self.assertTrue(verify_harness.can_exclude(m))
        self.assertFalse(verify_harness.can_exclude(method(["int[]"])))
        h = verify_harness.generate(m, [{"p0": -2147483648, "p1": None}, {"p0": 3, "p1": "a\"b"}])
        self.assertIn("CProver.assume(!(p0 == Integer.MIN_VALUE && p1 == null));", h.source)
        self.assertIn("p1.equals(new String(new char[] {97, 34, 98}))", h.source)

    def test_java_literal(self):
        self.assertEqual(verify_harness.java_literal("String", "ab"), '"ab"')
        self.assertEqual(verify_harness.java_literal("String", "あ\\"), "new String(new char[] {12354, 92})")
        self.assertEqual(verify_harness.java_literal("int[]", [1, -2]), "new int[] {1, -2}")
        self.assertEqual(verify_harness.java_literal("String[]", ["x", None]), 'new String[] {"x", null}')
        self.assertEqual(verify_harness.java_literal("boolean", True), "true")
        with self.assertRaises(ValueError):
            verify_harness.java_literal("int", None)

    def test_replay_prints_a_result_line(self):
        src = verify_harness.generate_replay(method(["int"], "int", name="f"), {"p0": 5})
        self.assertIn(verify_harness.REPLAY_RESULT_PREFIX, src)
        self.assertIn("jbmcverify.correct.p.C.f(", src)


def jbmc_output(*messages):
    return json.dumps([{"program": "JBMC 6.11.0"}, *messages, {"cProverStatus": "failure"}])


def failure(file, line, description="assertion"):
    return {"property": "x.assertion.1", "description": description, "status": "FAILURE",
            "trace": [{"stepType": "assignment", "lhs": "p0", "value": {"name": "integer", "data": "1"}},
                      {"stepType": "failure", "sourceLocation": {"file": file, "line": str(line)}}]}


class ParseOutputTests(unittest.TestCase):
    def test_pass(self):
        text = json.dumps([{"program": "JBMC"}, {"cProverStatus": "success"}])
        self.assertEqual(verify_jbmc.parse_output("full", 0, text, 30, 31).outcome, "pass")

    def test_harness_violations(self):
        run = verify_jbmc.parse_output("full", 10, jbmc_output(failure("VerifyHarness.java", 30)), 30, 31)
        self.assertEqual((run.outcome, run.assertion), ("harness_violation", "exception"))
        self.assertTrue(run.trace)
        run = verify_jbmc.parse_output("bounded", 10, jbmc_output(failure("VerifyHarness.java", 31)), 30, 31)
        self.assertEqual((run.outcome, run.assertion), ("harness_violation", "return"))
        self.assertEqual(run.kind, "bounded")

    def test_unwind_and_other_violations(self):
        out = jbmc_output(failure("p/C.java", 9, "unwinding assertion loop 0"))
        self.assertEqual(verify_jbmc.parse_output("full", 10, out, 30, 31).outcome, "unwind_violation")
        out = jbmc_output(failure("p/C.java", 9, "array index"))
        self.assertEqual(verify_jbmc.parse_output("full", 10, out, 30, 31).outcome, "other_violation")

    def test_timeout_memory_and_errors(self):
        self.assertEqual(verify_jbmc.parse_output("full", 124, "", 1, 2).outcome, "timeout")
        oom = json.dumps([{"messageType": "ERROR", "messageText": "Out of memory"}, {"cProverStatus": "error"}])
        self.assertEqual(verify_jbmc.parse_output("full", 6, oom, 1, 2).outcome, "memory")
        self.assertEqual(verify_jbmc.parse_output("full", 6, "garbage", 1, 2).outcome, "error")

    def test_build_command(self):
        opts = verify_jbmc.JbmcOptions()
        full = verify_jbmc.build_command(["a", "b"], opts, "full", 16)
        bounded = verify_jbmc.build_command(["a", "b"], opts, "bounded", 16)
        self.assertIn("--unwinding-assertions", full)
        self.assertIn("--no-unwinding-assertions", bounded)
        self.assertEqual(full[full.index("--unwind") + 1], "16")
        self.assertEqual(opts.unwind_schedule(), [8, 16, 32, 64])


class DecodeTests(unittest.TestCase):
    def test_real_trace_with_string_and_int_array(self):
        trace = json.loads((FIXTURES / "jbmc_trace_string_int_array.json").read_text(encoding="utf-8"))
        # The fixture ends with a call into the correct version followed by p0 = null,
        # which must be ignored.
        self.assertEqual(verify_jbmc.decode_counterexample(trace, ["p0", "p1"]), {"p0": "あ?", "p1": [0, 7]})

    def test_ints_null_and_missing(self):
        fn = {"function": "java::VerifyHarness.check:(II)V"}
        trace = [
            {"stepType": "assignment", "lhs": "p0", "sourceLocation": fn,
             "value": {"name": "integer", "type": "int", "data": "-1", "binary": "1" * 32}},
            {"stepType": "assignment", "lhs": "p1", "sourceLocation": fn, "value": {"name": "pointer", "data": "null"}},
        ]
        self.assertEqual(verify_jbmc.decode_counterexample(trace, ["p0", "p1"]), {"p0": -1, "p1": None})
        with self.assertRaises(verify_jbmc.DecodeError):
            verify_jbmc.decode_counterexample(trace, ["p0", "p2"])


class AggregateTests(unittest.TestCase):
    def r(self, status, reason=""):
        return MethodResult(method="m", return_type="int", status=status, reason=reason)

    def test_any_fail_wins(self):
        self.assertEqual(batch_verify._aggregate([self.r("unverifiable", "timeout"), self.r("fail", "return_mismatch"),
                                                  self.r("success")]), ("fail", "return_mismatch"))

    def test_unverifiable_reason_by_priority(self):
        results = [self.r("success"), self.r("unverifiable", "unwind_limit"),
                   self.r("unverifiable", "unsupported_signature")]
        self.assertEqual(batch_verify._aggregate(results), ("unverifiable", "unsupported_signature"))

    def test_success_only_when_all_succeed(self):
        self.assertEqual(batch_verify._aggregate([self.r("success"), self.r("success")]), ("success", ""))
        self.assertEqual(batch_verify._aggregate([]), ("unverifiable", "no_target_method"))


class CorrectCheckoutTests(unittest.TestCase):
    def fake_checkout(self, calls):
        def run(cmd, cwd=None, log=None, **kwargs):
            calls.append(cmd)
            work = Path(cmd[cmd.index("-w") + 1])
            work.mkdir(parents=True)
            (work / "Fresh.java").write_text("x")

            class Proc:
                returncode = 0
                stdout = stderr = ""

            return Proc()
        return run

    def test_checks_out_once_and_reuses_with_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, calls = Path(tmp) / "correct", []
            with patch.object(batch_verify, "_run", side_effect=self.fake_checkout(calls)):
                first = batch_verify.ensure_correct_checkout(root, "Math", "99", Path(tmp) / "log")
                second = batch_verify.ensure_correct_checkout(root, "Math", "99", Path(tmp) / "log")
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:6], ["defects4j", "checkout", "-p", "Math", "-v", "99f"])
            marker = json.loads((root / "Math-99.complete").read_text())
            self.assertEqual(marker["version"], "99f")

    def test_unfinished_checkout_is_redone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, calls = Path(tmp) / "correct", []
            (root / "Math-99").mkdir(parents=True)
            (root / "Math-99" / "Partial.java").write_text("x")
            with patch.object(batch_verify, "_run", side_effect=self.fake_checkout(calls)):
                checkout = batch_verify.ensure_correct_checkout(root, "Math", "99", Path(tmp) / "log")
            self.assertEqual(len(calls), 1)
            self.assertFalse((checkout / "Partial.java").exists())
            self.assertTrue((checkout / "Fresh.java").exists())
            self.assertTrue((root / "Math-99.complete").is_file())


class ResultsCsvTests(unittest.TestCase):
    def test_adds_columns_and_keeps_other_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Normal"
            target.mkdir()
            csv_file = target / "results.csv"
            csv_file.write_text("project,id,test_status\nMath,99,success\nLang,41,fail\n")

            def fake_row(target_dir, project, bug_id, config, correct_root):
                self.assertEqual(correct_root, Path(tmp).resolve() / "correct")
                return "fail", "return_mismatch", 1.5, {"methods": []}

            with patch.object(batch_verify, "verify_row", side_effect=fake_row) as row, \
                 patch.object(batch_verify.shutil, "which", return_value="/usr/bin/jbmc"), \
                 patch.object(batch_verify.verify_jbmc, "jbmc_version", return_value="6.11.0"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(batch_verify.run(str(target)), 0)
            self.assertEqual(row.call_count, 2)
            rows = list(csv.DictReader(csv_file.open()))
            self.assertEqual(list(rows[0]), ["project", "id", "test_status", "verify_status", "verify_reason",
                                             "verify_time_sec"])
            self.assertEqual((rows[0]["test_status"], rows[0]["verify_status"], rows[0]["verify_reason"],
                              rows[0]["verify_time_sec"]), ("success", "fail", "return_mismatch", "1.50"))
            self.assertEqual((rows[1]["test_status"], rows[1]["verify_status"]), ("fail", "fail"))


class PlanTests(unittest.TestCase):
    def run_plan(self, root, variants, defaults=None):
        plan = root / "plan.yaml"
        plan.write_text(yaml.safe_dump({"in_csv": "input.csv", "defaults": defaults or {}, "variants": variants}))
        errors = io.StringIO()
        with patch.object(pipeline.Path, "cwd", return_value=root), \
             patch.object(pipeline.batch_verify, "run", return_value=0) as verify, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            result = pipeline.run_plan(str(plan), None)
        return result, verify, errors.getvalue()

    def test_verify_only_runs_on_existing_dir_and_appends_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            out = root / "runs/normal"
            out.mkdir(parents=True)
            (out / "run_all_results.txt").write_text("original log\n")
            result, verify, _ = self.run_plan(root, [{"name": "normal", "out_dir": "runs/normal"}], {
                "steps": ["verify"], "verify": {"timeout": 60, "correct_dir": "runs/correct"}})
            self.assertEqual(result, 0)
            verify.assert_called_once()
            out_dir, config = verify.call_args.args
            self.assertEqual(out_dir, str(out))
            self.assertEqual(config.timeout_sec, 60)
            self.assertEqual(config.correct_dir, str(root / "runs/correct"))
            self.assertTrue((out / "run_all_results.txt").read_text().startswith("original log\n"))

    def test_checkout_variant_still_needs_a_new_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "runs/normal").mkdir(parents=True)
            with patch.object(pipeline.batch_checkout, "run", return_value=0) as checkout:
                result, verify, errors = self.run_plan(root, [{"name": "normal", "out_dir": "runs/normal"}],
                                                       {"steps": ["checkout", "verify"]})
            self.assertEqual(result, 2)
            checkout.assert_not_called()
            verify.assert_not_called()
            self.assertIn("already exists", errors)

    def test_unknown_verify_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "runs/normal").mkdir(parents=True)
            result, verify, errors = self.run_plan(root, [{"name": "normal", "out_dir": "runs/normal"}],
                                                   {"steps": ["verify"], "verify": {"unwnd": 3}})
            self.assertEqual(result, 2)
            verify.assert_not_called()
            self.assertIn("unknown verify keys", errors)


if __name__ == "__main__":
    unittest.main()
