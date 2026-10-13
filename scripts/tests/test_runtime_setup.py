from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import runtime_setup
from scripts.utils.sync_timing import StageTimings


class RuntimeSetupTest(unittest.TestCase):
    def test_installed_dependencies_and_browser_skip_installation(self):
        with patch.object(runtime_setup.importlib.util, "find_spec", return_value=Mock()), \
             patch.object(runtime_setup, "chromium_available", return_value=True), \
             patch.object(runtime_setup, "install") as install, redirect_stdout(io.StringIO()):
            runtime_setup.prepare_runtime(browser_required=True)
        install.assert_not_called()

    def test_missing_dependencies_are_installed_once_before_browser_check(self):
        with patch.object(runtime_setup.importlib.util, "find_spec", return_value=None), \
             patch.object(runtime_setup, "chromium_available", return_value=False), \
             patch.object(runtime_setup, "install") as install, redirect_stdout(io.StringIO()):
            runtime_setup.prepare_runtime(browser_required=True)
        self.assertEqual(install.call_count, 2)
        self.assertIn("requirements.txt", str(install.call_args_list[0]))
        self.assertEqual(install.call_args_list[1].args[0], ["-m", "playwright", "install", "chromium"])

    def test_password_refresh_does_not_require_browser(self):
        with patch.object(runtime_setup.importlib.util, "find_spec", return_value=Mock()), \
             patch.object(runtime_setup, "chromium_available") as browser, \
             patch.object(runtime_setup, "install") as install, redirect_stdout(io.StringIO()):
            runtime_setup.prepare_runtime(browser_required=False)
        browser.assert_not_called()
        install.assert_not_called()

    def test_default_preparation_skips_browser(self):
        with patch.object(runtime_setup.importlib.util, "find_spec", return_value=Mock()), \
             patch.object(runtime_setup, "chromium_available") as browser, \
             patch.object(runtime_setup, "install") as install, redirect_stdout(io.StringIO()):
            runtime_setup.prepare_runtime()
        browser.assert_not_called()
        install.assert_not_called()

    def test_parallel_preparation_installs_shared_dependencies_once(self):
        code = '''
import pathlib, sys, time
from scripts import runtime_setup
root = pathlib.Path(sys.argv[1])
runtime_setup.PROJECT_ROOT = root
runtime_setup.importlib.util.find_spec = lambda name: object() if (root / "installed").exists() else None
runtime_setup.chromium_available = lambda: True
def install(arguments):
    with (root / "installations").open("a") as output:
        output.write("install\\n")
    time.sleep(0.15)
    (root / "installed").touch()
runtime_setup.install = install
runtime_setup.prepare_runtime(browser_required=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            processes = [subprocess.Popen([sys.executable, "-c", code, directory],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for _ in range(2)]
            try:
                for process in processes:
                    output, errors = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, errors)
                self.assertEqual((Path(directory) / "installations").read_text().splitlines(), ["install"])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_browser_check_checks_the_installed_executable(self):
        factory = MagicMock()
        browser = factory.return_value.__enter__.return_value.chromium
        with tempfile.TemporaryDirectory() as directory, \
             patch("playwright.sync_api.sync_playwright", factory):
            executable = Path(directory) / "chromium"
            browser.executable_path = str(executable)
            self.assertFalse(runtime_setup.chromium_available())
            executable.touch()
            self.assertTrue(runtime_setup.chromium_available())

    def test_browser_check_detects_missing_headless_components(self):
        factory = MagicMock()
        browser = factory.return_value.__enter__.return_value.chromium
        with tempfile.TemporaryDirectory() as directory, patch("playwright.sync_api.sync_playwright", factory):
            executable = Path(directory) / "chromium"
            executable.touch()
            browser.executable_path = str(executable)
            browser.launch.side_effect = RuntimeError("synthetic-missing-headless-browser")
            self.assertFalse(runtime_setup.chromium_available())

    def test_install_errors_hide_private_subprocess_output(self):
        with patch.object(runtime_setup.subprocess, "run", return_value=Mock(returncode=1, stdout="synthetic-private-value")):
            with self.assertRaises(RuntimeError) as raised:
                runtime_setup.install(["-m", "pip", "install", "playwright"])
        self.assertNotIn("synthetic-private-value", str(raised.exception))

    def test_install_timeout_is_reported_without_subprocess_output(self):
        error = subprocess.TimeoutExpired(["python"], 300, output="synthetic-private-value")
        with patch.object(runtime_setup.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                runtime_setup.install(["-m", "pip", "install", "playwright"])
        self.assertNotIn("synthetic-private-value", str(raised.exception))


class StageTimingsTest(unittest.TestCase):
    def test_reports_cumulative_phase_time_and_total_wall_time_on_failure(self):
        clock = Mock(side_effect=[0, 1, 3, 4, 7, 10])
        timings = StageTimings(clock=clock)
        with timings.measure("下载"):
            pass
        with self.assertRaises(ValueError), timings.measure("下载"):
            raise ValueError("synthetic-private-value")
        output = io.StringIO()
        timings.write(output)
        self.assertIn("下载累计 5.00s", output.getvalue())
        self.assertIn("总耗时 10.00s", output.getvalue())
        self.assertNotIn("synthetic-private-value", output.getvalue())
