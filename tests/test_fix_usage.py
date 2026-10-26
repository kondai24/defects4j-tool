import asyncio
import csv
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_agent_sdk import ResultMessage
from openai_codex.generated.v2_all import ThreadTokenUsage, TokenUsageBreakdown

from workspace.defects4j_tools import batch_fix, fix_claude, fix_codex, timing
from workspace.defects4j_tools.fix_common import FixResponse, FixSessionError


class ProviderUsageTests(unittest.IsolatedAsyncioTestCase):
    async def collect_claude(self, **overrides):
        fields = dict(subtype="success", duration_ms=100, duration_api_ms=90,
                      is_error=False, num_turns=3, session_id="test",
                      total_cost_usd=0.012345, result="fixed",
                      usage={"input_tokens": 120, "output_tokens": 30})
        fields.update(overrides)

        async def query(**kwargs):
            yield object()  # Intermediate messages must not be counted again.
            yield ResultMessage(**fields)

        with patch.object(fix_claude, "query", query):
            return await fix_claude._collect_result("prompt", None)

    async def test_claude_cumulative_usage(self):
        response = await self.collect_claude()
        self.assertEqual((response.total_cost_usd, response.num_turns,
                          response.input_tokens, response.output_tokens),
                         (0.012345, 3, 120, 30))

    async def test_claude_missing_usage_is_unknown(self):
        response = await self.collect_claude(usage=None, total_cost_usd=None)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.total_cost_usd)

    async def test_claude_error_retains_usage(self):
        with self.assertRaises(FixSessionError) as caught:
            await self.collect_claude(is_error=True, errors=["limit"])
        self.assertEqual(caught.exception.response.input_tokens, 120)
        self.assertIn("limit", str(caught.exception))

    async def test_codex_uses_total_instead_of_last(self):
        def breakdown(i, o):
            return TokenUsageBreakdown(input_tokens=i, output_tokens=o,
                                       cached_input_tokens=40, reasoning_output_tokens=5,
                                       total_tokens=i + o)

        usage = ThreadTokenUsage(total=breakdown(800, 100), last=breakdown(80, 10))
        result = SimpleNamespace(items=[], usage=usage, final_response="fixed", error=None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(fix_codex, "_run_turn", AsyncMock(return_value=result)):
                response = await fix_codex.run_fix(bug_cwd=root, prompt_md="fix", target_file="A.java",
                                                   model="test", log_path=root / "log.jsonl")
        self.assertEqual((response.input_tokens, response.output_tokens), (800, 100))
        self.assertIsNone(response.total_cost_usd)
        self.assertIsNone(response.num_turns)

    async def test_codex_missing_usage(self):
        result = SimpleNamespace(items=[], usage=None, final_response="fixed", error=None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(fix_codex, "_run_turn", AsyncMock(return_value=result)):
                response = await fix_codex.run_fix(bug_cwd=root, prompt_md="fix", target_file="A.java",
                                                   model="test", log_path=root / "log.jsonl")
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)


class CsvUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.csv = self.root / "results.csv"
        self.csv.write_text("project,id,test_status,prompt_time_sec\nLang,1,Success,2.00\nMath,2,Failure,3.00\n")

    def rows(self):
        with self.csv.open(newline="") as fh:
            return list(csv.DictReader(fh))

    def test_write_preserves_rows_columns_precision_and_zero(self):
        timing.record_fix_usage(self.csv, "Lang", "1", FixResponse(None, total_cost_usd=0.000001,
                                                                 num_turns=2, input_tokens=123, output_tokens=0))
        timing.record_time(self.csv, "Lang", "1", timing.FIX_TIME_COL, 1.25)
        first, second = self.rows()
        self.assertEqual([first[c] for c in timing.FIX_USAGE_COLUMNS], ["1e-06", "2", "123", "0"])
        self.assertEqual(first["test_status"], "Success")
        self.assertEqual(first["prompt_fix_time_sec"], "3.25")
        self.assertEqual(second["test_status"], "Failure")
        self.assertTrue(all(second[c] == "" for c in timing.FIX_USAGE_COLUMNS))

    def test_rerun_clears_all_usage_and_preserves_prompt_time(self):
        timing.record_fix_usage(self.csv, "Lang", "1", FixResponse(None, total_cost_usd=1, num_turns=3))
        timing.clear_fix_usage(self.csv)
        for row in self.rows():
            self.assertTrue(all(row[c] == "" for c in timing.FIX_USAGE_COLUMNS))
        self.assertEqual(self.rows()[0]["prompt_time_sec"], "2.00")

    def test_batch_rerun_clears_usage_even_for_skipped_bugs(self):
        timing.record_fix_usage(self.csv, "Lang", "1", FixResponse(None, input_tokens=123, num_turns=3))
        with patch.object(batch_fix.bug_locator, "resolve_bug_dir", return_value=None):
            status = asyncio.run(batch_fix._run_all(
                self.root, self.csv, self.rows(), provider="codex", model="test"))
        self.assertEqual(status, 0)
        for row in self.rows():
            self.assertTrue(all(row[c] == "" for c in timing.FIX_USAGE_COLUMNS))
        self.assertEqual(self.rows()[0]["prompt_time_sec"], "2.00")

    def test_batch_saves_success_error_and_timeout(self):
        java = self.root / "A.java"
        prompt = self.root / "A_prompt.md"
        java.write_text("class A {}")
        prompt.write_text("fix")
        response = FixResponse("done", total_cost_usd=0.01, num_turns=2, input_tokens=100, output_tokens=10)
        cases = [(None, "unchanged", "100"),
                 (FixSessionError("failed", response), "error", "100"),
                 (RuntimeError("SDK failure"), "error", ""),
                 (asyncio.TimeoutError(), "timeout", "")]
        for error, expected_status, tokens in cases:
            with self.subTest(status=expected_status, error=error):
                timing.clear_fix_usage(self.csv)
                runner = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
                with patch.object(batch_fix, "_run_provider", runner):
                    status = asyncio.run(batch_fix._run_fix_for_one(
                        1, 1, "claude", "Lang", "1", java, prompt, "test", io.StringIO(), self.csv, {}))
                self.assertEqual(status, expected_status)
                self.assertEqual(self.rows()[0]["input_tokens"], tokens)
                self.assertNotEqual(self.rows()[0]["fix_time_sec"], "")


if __name__ == "__main__":
    unittest.main()
