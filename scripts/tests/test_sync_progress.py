import importlib
import io
import unittest
from unittest.mock import Mock


class SyncProgressTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.utils.sync_progress")
        self.output = io.StringIO()

    def test_progress_counts_success_and_failure_and_always_emits_final_total(self):
        progress = self.module.SyncProgress(2, stream=self.output, interactive=False)
        progress.advance(True)
        progress.advance(False)
        self.assertEqual((progress.completed, progress.succeeded, progress.failed), (2, 1, 1))
        self.assertIn("0/2", self.output.getvalue())
        self.assertIn("2/2 (100%) 成功 1 失败 1", self.output.getvalue())

    def test_non_terminal_progress_is_limited_to_percentage_snapshots(self):
        progress = self.module.SyncProgress(1000, stream=self.output, interactive=False)
        for _ in range(1000):
            progress.advance(True)
        self.assertLessEqual(len(self.output.getvalue().splitlines()), 21)
        self.assertIn("1000/1000 (100%)", self.output.getvalue())

    def test_interactive_progress_is_time_throttled_but_finishes_immediately(self):
        clock = Mock(return_value=0)
        progress = self.module.SyncProgress(3, stream=self.output, interactive=True, clock=clock)
        progress.advance(True)
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        clock.return_value = 0.21
        progress.advance(False)
        self.assertEqual(len(self.output.getvalue().splitlines()), 2)
        progress.advance(True)
        self.assertIn("3/3 (100%)", self.output.getvalue())
        self.assertEqual(len(self.output.getvalue().splitlines()), 3)

    def test_empty_progress_is_complete_and_cannot_overcount(self):
        progress = self.module.SyncProgress(0, stream=self.output)
        self.assertIn("0/0 (100%)", self.output.getvalue())
        with self.assertRaises(ValueError):
            progress.advance(True)

    def test_failures_are_grouped_into_a_bounded_final_summary(self):
        failures = self.module.FailureSummary()
        for _ in range(50):
            failures.add("上传", "RuntimeError")
        for index in range(6):
            failures.add("导入 COROS", f"HTTP {500 + index}")
        failures.write(stream=self.output)
        lines = self.output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 4)
        self.assertIn("上传失败：RuntimeError（50 条）", lines[0])
        self.assertIn("其他失败：4 条", lines[-1])

    def test_successful_batch_has_no_failure_summary(self):
        self.module.FailureSummary().write(stream=self.output)
        self.assertEqual(self.output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
