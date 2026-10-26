import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
import pipeline
from defects4j_tools.fix_common import FixResponse


class PlanEffortTests(unittest.TestCase):
    def test_plan_settings_reach_claude_sdk_and_log(self):
        cases = [({}, "claude-opus-5-5", "medium"),
                 ({"model": "claude-sonnet-5-5", "effort": "high"}, "claude-sonnet-5-5", "high"),
                 ({"effort": None}, "claude-opus-5-5", None)]
        for override, model, effort in cases:
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                out_dir = root / "trial"
                java = root / "A.java"
                java.write_text("class A {}")
                (root / "A_prompt.md").write_text("fix")
                plan = {"in_csv": "input.csv", "defaults": {
                    "steps": ["fix"], "model": "claude-opus-5-5", "effort": "medium"},
                    "variants": [{"name": "trial", "out_dir": str(out_dir), **override}]}
                plan_file = root / "plan.yaml"
                plan_file.write_text(yaml.safe_dump(plan))
                collect = AsyncMock(return_value=FixResponse("done"))
                batch = pipeline.batch_fix
                run_fix = batch.run

                def prepare_and_fix(target_dir, **kwargs):
                    (Path(target_dir) / "results.csv").write_text("project,id\nCli,1\n")
                    return run_fix(target_dir, **kwargs)

                with patch.dict(os.environ, {"FIX_PROVIDER": "claude", "ANTHROPIC_API_KEY": "test"}), \
                     patch.object(batch, "run", side_effect=prepare_and_fix), \
                     patch.object(batch.fix_claude, "_collect_result", collect), \
                     patch.object(batch.bug_locator, "resolve_bug_dir", return_value=root), \
                     patch.object(batch.bug_locator, "run_defects4j_export_classes_modified", return_value="A"), \
                     patch.object(batch.bug_locator, "find_java_file", return_value=(java, None)), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pipeline.run_plan(str(plan_file), None), 0)
                options = collect.await_args.args[1]
                self.assertEqual(options.model, model)
                self.assertEqual(options.effort, effort)
                log = (out_dir / "run_all_results.txt").read_text()
                self.assertIn(f"model: {model}", log)
                self.assertIn(f"effort: {effort or 'default'}", log)

    def test_invalid_effort_stops_before_checkout(self):
        for effort, provider in [("typo", "claude"), (42, "claude"), ("high", "codex")]:
            with self.subTest(effort=effort, provider=provider), tempfile.TemporaryDirectory() as tmp:
                plan = {"in_csv": "input.csv", "defaults": {
                    "model": "test", "effort": effort},
                    "variants": [{"name": "trial", "out_dir": str(Path(tmp) / "trial")}]}
                path = Path(tmp) / "plan.yaml"
                path.write_text(yaml.safe_dump(plan))
                with patch.dict(os.environ, {"FIX_PROVIDER": provider}), \
                     patch.object(pipeline.batch_checkout, "run") as checkout, \
                     contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(pipeline.run_plan(str(path), None), 2)
                checkout.assert_not_called()
                self.assertIn("effort", errors.getvalue())

    def test_omitted_effort_keeps_sdk_default(self):
        collect = AsyncMock(return_value=FixResponse("done"))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(pipeline.batch_fix.fix_claude, "_collect_result", collect):
            import asyncio
            asyncio.run(pipeline.batch_fix.fix_claude.run_fix(
                bug_cwd=Path(tmp), prompt_md="fix", target_file="A.java", model="test"))
        self.assertIsNone(collect.await_args.args[1].effort)


if __name__ == "__main__":
    unittest.main()
