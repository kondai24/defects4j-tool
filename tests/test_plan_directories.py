import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
import pipeline


class PlanDirectoryTests(unittest.TestCase):
    def run_plan(self, root, variants, only=None):
        plan = root / "plan.yaml"
        plan.write_text(yaml.safe_dump({
            "in_csv": "input.csv",
            "defaults": {"steps": ["checkout"], "continue_on_error": True},
            "variants": variants,
        }))
        errors = io.StringIO()
        with patch.object(pipeline.Path, "cwd", return_value=root), \
             patch.object(pipeline.batch_checkout, "run", return_value=0) as checkout, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(errors):
            result = pipeline.run_plan(str(plan), only)
        return result, checkout, errors.getvalue()

    def test_creates_nested_outputs_and_runs_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result, checkout, _ = self.run_plan(root, [
                {"name": "ast", "out_dir": "runs/trial_opus55_ast"},
                {"name": "normal", "out_dir": "runs/trial_opus55_normal"},
            ])
            self.assertEqual(result, 0)
            self.assertEqual(checkout.call_count, 2)
            for call in checkout.call_args_list:
                output, csv = call.args
                self.assertTrue((Path(output) / "run_all_results.txt").is_file())
                self.assertEqual(csv, str(root / "input.csv"))

    def test_existing_later_output_stops_everything_without_overwriting(self):
        for kind in ["empty_dir", "dir_with_log", "file"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                existing = root / "runs/ast"
                existing.parent.mkdir()
                if kind == "file":
                    existing.write_text("keep")
                else:
                    existing.mkdir()
                    if kind == "dir_with_log":
                        (existing / "run_all_results.txt").write_text("keep")
                result, checkout, errors = self.run_plan(root, [
                    {"name": "normal", "out_dir": "runs/normal"},
                    {"name": "ast", "out_dir": "runs/ast"},
                ])
                self.assertEqual(result, 2)
                checkout.assert_not_called()
                self.assertFalse((root / "runs/normal").exists())
                self.assertIn("already exists", errors)
                if kind == "file":
                    self.assertEqual(existing.read_text(), "keep")
                elif kind == "dir_with_log":
                    self.assertEqual((existing / "run_all_results.txt").read_text(), "keep")

    def test_only_ignores_unselected_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "runs/normal").mkdir(parents=True)
            result, checkout, _ = self.run_plan(root, [
                {"name": "normal", "out_dir": "runs/normal"},
                {"name": "ast", "out_dir": "runs/ast"},
            ], only="ast")
            self.assertEqual(result, 0)
            checkout.assert_called_once_with(str(root / "runs/ast"), str(root / "input.csv"))

    def test_duplicate_outputs_stop_before_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result, checkout, errors = self.run_plan(root, [
                {"name": "first", "out_dir": "runs/ast"},
                {"name": "second", "out_dir": "runs/./ast"},
            ])
            self.assertEqual(result, 2)
            checkout.assert_not_called()
            self.assertFalse((root / "runs").exists())
            self.assertIn("duplicate out_dir", errors)


if __name__ == "__main__":
    unittest.main()
