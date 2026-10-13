import asyncio
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import account_config, account_state, sync_accounts
from scripts.tests.test_account_config import account_data


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class SyncAccountsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.local.json"
        self.state_root = self.root / "states"
        self.worker = self.root / "worker.py"
        self.worker.write_text(
            "import json, os, pathlib, sys\n"
            "from scripts import config\n"
            "state = pathlib.Path(config.DB_DIR)\n"
            "state.mkdir(parents=True, exist_ok=True)\n"
            "(state / 'worker.json').write_text(json.dumps({\n"
            " 'pid': os.getpid(), 'args': sys.argv[1:],\n"
            " 'email': os.environ['GARMIN_EMAIL'],\n"
            " 'direction': os.environ['SYNC_DIRECTION'],\n"
            " 'headless': os.environ.get('GARMIN_BROWSER_HEADLESS'),\n"
            " 'progress_tty': os.environ.get('SYNC_PROGRESS_TTY'),\n"
            " 'full_scan': os.environ.get('SYNC_FULL_SCAN'),\n"
            " 'token': config.GARTH_TOKEN_FILE, 'fit': config.GARMIN_FIT_DIR,\n"
            " 'legacy_file': os.environ.get('GARMIN_ENV_FILE'),\n"
            " 'inline_config': os.environ.get('SYNC_ACCOUNTS_JSON')}))\n"
            "print(os.environ['GARMIN_EMAIL'], os.environ['GARMIN_PASSWORD'])\n"
            "print('remote login token=synthetic-token')\n"
            "sys.exit(1 if os.environ.get('SYNC_ACCOUNT_ID') == 'account_a' else 0)\n",
            encoding="utf-8",
        )
        self.write_config()
        setup = patch.object(sync_accounts, "prepare_runtime", create=True)
        self.prepare_runtime = setup.start()
        self.addCleanup(setup.stop)
        patcher = patch.dict(os.environ, {"PYTHONPATH": str(account_config.runtime_config.PROJECT_ROOT)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, disabled=False):
        first, second = account_data(), account_data("account_b")
        first["enabled"] = not disabled
        second["direction"] = "coros_to_garmin"
        self.config.write_text(json.dumps({"accounts": [first, second]}))

    def run_main(self, args):
        output = io.StringIO()
        with patch.object(sync_accounts, "DEFAULT_STATE_ROOT", self.state_root), \
             patch.object(sync_accounts, "worker_command", return_value=[sys.executable, str(self.worker)]), \
             redirect_stdout(output), redirect_stderr(output):
            code = sync_accounts.main(["--config", str(self.config), *args])
        return code, output.getvalue()

    def test_parallel_subprocesses_isolate_state_and_continue_after_failure(self):
        code, output = self.run_main(["--headless-only"])
        self.assertEqual(code, 1)
        results = [json.loads((self.state_root / account_id / "worker.json").read_text())
                   for account_id in ("account_a", "account_b")]
        self.assertNotEqual(results[0]["pid"], results[1]["pid"])
        for account_id, result in zip(("account_a", "account_b"), results):
            self.assertEqual(result["email"], f"{account_id}@example.invalid")
            self.assertTrue(Path(result["token"]).is_relative_to((self.state_root / account_id).resolve()))
            self.assertIsNone(result["legacy_file"])
            self.assertIsNone(result["inline_config"])
        self.assertEqual(results[1]["direction"], "coros_to_garmin")
        self.assertIn("成功 1 组，失败 1 组", output)
        self.assertNotIn("synthetic-garmin-password", output)
        self.assertNotIn("@example.invalid", output)
        self.assertNotIn("synthetic-token", output)
        self.assertLess(output.index("[account_b] 开始"), output.index("[account_a] 失败"))
        self.prepare_runtime.assert_called_once_with(browser_required=False)
        self.assertIn("账号总耗时：", output)

    def test_background_log_timestamps_cover_start_worker_output_and_summary(self):
        code, output = self.run_main(["--headless-only"])
        self.assertEqual(code, 1)
        for marker in ("[account_a] 开始", "[account_a] remote login", "[account_a] 失败", "同步汇总："):
            line = next(line for line in output.splitlines() if marker in line)
            self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] ")

    def test_list_does_not_prepare_runtime_and_password_refresh_skips_browser(self):
        self.run_main(["--list"])
        self.prepare_runtime.assert_not_called()
        self.run_main(["--account", "account_b", "--refresh-token", "password"])
        self.prepare_runtime.assert_called_once_with(browser_required=False)

    def test_browser_actions_defer_browser_preparation_to_login_worker(self):
        for args in (["--browser-only"], ["--refresh-token", "browser"], []):
            with self.subTest(args=args):
                self.prepare_runtime.reset_mock()
                self.run_main(["--account", "account_b", *args])
                self.prepare_runtime.assert_called_once_with(browser_required=False)

    def write_waiting_worker(self):
        self.worker.write_text(
            "import json, os, pathlib, sys, time\n"
            "root = pathlib.Path(os.environ['SYNC_STATE_ROOT'])\n"
            "state = root / os.environ['SYNC_ACCOUNT_ID']\n"
            "started = time.monotonic()\n"
            "(state / 'ready').touch()\n"
            "deadline = started + 3\n"
            "while len(list(root.glob('*/ready'))) < 2 and time.monotonic() < deadline:\n"
            " time.sleep(0.01)\n"
            "time.sleep(0.05)\n"
            "(state / 'times.json').write_text(json.dumps([started, time.monotonic()]))\n"
            "print('同步进度：1/1')\n"
            "sys.stdout.write('尾行 password=synthetic-private-value')\n",
            encoding="utf-8",
        )

    def test_default_workers_overlap_in_time_and_forward_partial_final_lines(self):
        self.write_waiting_worker()
        code, output = self.run_main(["--headless-only"])
        self.assertEqual(code, 0)
        intervals = [json.loads((self.state_root / account_id / "times.json").read_text())
                     for account_id in ("account_a", "account_b")]
        self.assertLess(max(start for start, end in intervals), min(end for start, end in intervals))
        for account_id in ("account_a", "account_b"):
            self.assertIn(f"[{account_id}] 尾行 password=[REDACTED]", output)
        self.assertNotIn("synthetic-private-value", output)

    def test_jobs_one_runs_accounts_serially(self):
        code, output = self.run_main(["--headless-only", "--jobs", "1"])
        self.assertEqual(code, 1)
        self.assertLess(output.index("[account_a] 失败"), output.index("[account_b] 开始"))

    def test_full_scan_is_forwarded_only_when_explicitly_selected(self):
        self.assertEqual(self.run_main(["--account", "account_b", "--full-scan"])[0], 0)
        result = json.loads((self.state_root / "account_b" / "worker.json").read_text())
        self.assertEqual(result["full_scan"], "1")
        with patch.dict(os.environ, {"SYNC_FULL_SCAN": "1"}):
            self.assertEqual(self.run_main(["--account", "account_b"])[0], 0)
        result = json.loads((self.state_root / "account_b" / "worker.json").read_text())
        self.assertEqual(result["full_scan"], "0")

    def test_jobs_limit_is_respected_and_next_account_fills_a_free_slot(self):
        groups = [account_data(f"account_{index}") for index in range(5)]
        self.config.write_text(json.dumps({"accounts": groups}))
        self.write_waiting_worker()
        code, output = self.run_main(["--headless-only", "--jobs", "2", "--direction", "coros_to_garmin"])
        self.assertEqual(code, 0)
        intervals = [json.loads((self.state_root / group["id"] / "times.json").read_text())
                     for group in groups]
        events = sorted([(start, 1) for start, end in intervals] + [(end, -1) for start, end in intervals])
        active = peak = 0
        for timestamp, change in events:
            active += change
            peak = max(peak, active)
        self.assertEqual(peak, 2)
        self.assertIn("成功 5 组，失败 0 组", output)
        self.assertIn("[account_4] 开始 coros_to_garmin", output)

    def test_jobs_must_be_a_positive_integer_before_starting_workers(self):
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.run_main(["--jobs", value])
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.state_root.exists())

    def test_parallel_terminal_output_preserves_lines_and_disables_worker_tty(self):
        output = TerminalOutput()
        with patch.object(sync_accounts, "DEFAULT_STATE_ROOT", self.state_root), \
             patch.object(sync_accounts, "worker_command", return_value=[sys.executable, str(self.worker)]), \
             redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(sync_accounts.main(["--config", str(self.config)]), 1)
        self.assertNotIn("\r", output.getvalue())
        for account_id in ("account_a", "account_b"):
            state = json.loads((self.state_root / account_id / "worker.json").read_text())
            self.assertEqual(state["progress_tty"], "0")

    def test_locked_account_does_not_block_other_parallel_workers(self):
        account = account_config.load_accounts(self.config, env={})[0]
        with account_state.lock_state(account, self.state_root):
            code, output = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("该账号组正在运行", output)
        self.assertTrue((self.state_root / "account_b" / "worker.json").exists())

    def test_cancellation_stops_process_groups_releases_locks_and_skips_queued_accounts(self):
        groups = [account_data(f"account_{index}") for index in range(3)]
        self.config.write_text(json.dumps({"accounts": groups}))
        self.worker.write_text(
            "import json, os, pathlib, signal, subprocess, sys, time\n"
            "state = pathlib.Path(os.environ['SYNC_STATE_ROOT']) / os.environ['SYNC_ACCOUNT_ID']\n"
            "child = subprocess.Popen([sys.executable, '-c',\n"
            " 'import pathlib, signal, sys, time; '\n"
            " 'signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path(sys.argv[1]).touch(), sys.exit(0))); '\n"
            " 'pathlib.Path(sys.argv[2]).touch(); time.sleep(30)',\n"
            " str(state / 'child_stopped'), str(state / 'child_ready')])\n"
            "def stop(*args):\n"
            " child.wait(timeout=5)\n"
            " sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "while not (state / 'child_ready').exists(): time.sleep(0.01)\n"
            "(state / 'pid').write_text(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        accounts = account_config.load_accounts(self.config, env={})

        async def cancel_running_accounts():
            task = asyncio.create_task(sync_accounts.run_accounts(accounts, "--headless-only", None, 2))
            try:
                deadline = time.monotonic() + 10
                while len(list(self.state_root.glob('*/pid'))) < 2:
                    if time.monotonic() > deadline:
                        self.fail("Workers did not start")
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with patch.object(sync_accounts, "DEFAULT_STATE_ROOT", self.state_root), \
             patch.object(sync_accounts, "worker_command", return_value=[sys.executable, str(self.worker)]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            asyncio.run(cancel_running_accounts())
        for account in accounts[:2]:
            state = self.state_root / account.id
            self.assertTrue((state / "child_stopped").exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int((state / "pid").read_text()), 0)
            with account_state.lock_state(account, self.state_root):
                pass
        self.assertFalse((self.state_root / accounts[2].id).exists())

    def test_selection_runs_only_requested_group_and_reports_unknown_or_disabled(self):
        code, output = self.run_main(["--account", "account_b", "--browser-only"])
        self.assertEqual(code, 0)
        self.assertFalse((self.state_root / "account_a").exists())
        self.assertEqual(self.run_main(["--account", "unknown"])[0], 2)
        self.write_config(disabled=True)
        self.assertEqual(self.run_main(["--account", "account_a"])[0], 2)

    def test_disabled_groups_are_skipped_and_list_does_not_start_workers(self):
        self.write_config(disabled=True)
        code, output = self.run_main(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("account_b", output)
        self.assertNotIn("@example.invalid", output)
        self.assertFalse(self.state_root.exists())
        self.assertEqual(self.run_main([])[0], 0)
        self.assertFalse((self.state_root / "account_a").exists())

    def test_rebound_account_fails_without_running_worker_but_other_group_continues(self):
        self.run_main([])
        data = json.loads(self.config.read_text())
        data["accounts"][0]["coros"]["email"] = "changed@example.invalid"
        self.config.write_text(json.dumps(data))
        marker = self.state_root / "account_a" / "worker.json"
        original = marker.read_bytes()
        code, output = self.run_main([])
        self.assertEqual(code, 1)
        self.assertEqual(marker.read_bytes(), original)
        self.assertIn("account_b", output)

    def test_corrected_account_with_unused_database_reaches_its_own_worker(self):
        profile = account_config.load_accounts(self.config, env={})[1]
        with account_state.lock_state(profile, self.state_root) as state:
            with closing(sqlite3.connect(state / "garmin.db")) as database:
                with database:
                    database.execute("CREATE TABLE garmin_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER)")
        original_database = (state / "garmin.db").read_bytes()
        data = json.loads(self.config.read_text())
        data["accounts"][1]["garmin"]["email"] = "corrected@example.invalid"
        self.config.write_text(json.dumps(data))
        code, output = self.run_main(["--account", "account_b", "--headless-only"])
        self.assertEqual(code, 0)
        result = json.loads((state / "worker.json").read_text())
        self.assertEqual(result["email"], "corrected@example.invalid")
        self.assertEqual((state / "garmin.db").read_bytes(), original_database)
        self.assertFalse((self.state_root / "account_a").exists())

    def test_worker_command_preserves_existing_modes_and_never_puts_secrets_in_args(self):
        for mode in ("default", "--headless-only", "--browser-only"):
            command = sync_accounts.worker_command(mode)
            self.assertEqual(Path(command[1]).name, "run_sync.sh")
            self.assertEqual(command[2:], [] if mode == "default" else [mode])

    def test_empty_inline_json_and_missing_explicit_config_fail_closed(self):
        with patch.dict(os.environ, {"SYNC_ACCOUNTS_JSON": ""}):
            with self.assertRaises(ValueError):
                account_config.load_accounts(env=os.environ)
        self.config.unlink()
        self.assertEqual(self.run_main([])[0], 2)

    def test_worker_always_receives_account_scoped_paths(self):
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        environment = account_config.worker_environment(profile, {}, self.state_root)
        self.assertEqual(environment["SYNC_ACCOUNT_ID"], "account_a")
        self.assertEqual(Path(environment["SYNC_STATE_ROOT"]), self.state_root.resolve())
        self.assertEqual(environment["SYNC_ACCOUNT_WORKER"], "1")

    def test_launcher_help_works_without_credentials_or_dependencies(self):
        result = subprocess.run(["bash", "run_sync.sh", "--help"],
                                cwd=account_config.runtime_config.PROJECT_ROOT,
                                capture_output=True, text=True,
                                env={"PYTHON_BIN": sys.executable}, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--account", result.stdout)
        self.assertIn("--migrate-legacy", result.stdout)

    def test_json_configuration_is_required_before_any_worker_starts(self):
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        with patch.dict(os.environ, profile.env, clear=True), \
             patch.object(account_config, "DEFAULT_CONFIG_FILE", self.root / "absent.json"), \
             patch.object(sync_accounts, "run_worker", return_value=0) as worker, \
             redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(sync_accounts.main(["--headless-only"]), 2)
        worker.assert_not_called()
        self.assertFalse(self.state_root.exists())

    def test_token_refresh_uses_selected_group_state_and_explicit_headless_mode(self):
        code, output = self.run_main(["--account", "account_b", "--refresh-token", "browser", "--headless-only"])
        self.assertEqual(code, 0)
        self.assertFalse((self.state_root / "account_a").exists())
        result = json.loads((self.state_root / "account_b" / "worker.json").read_text())
        self.assertEqual(result["headless"], "1")
        for operation, script in (("password", "generate_garth_token.py"),
                                  ("browser", "generate_garth_token_browser.py")):
            command = sync_accounts.worker_command("default", operation)
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(Path(command[1]).name, script)

    def test_migration_cli_runs_only_the_explicit_migration_operation(self):
        with patch.object(sync_accounts, "migrate_configuration", return_value=self.root / "backup") as migrate:
            code, output = self.run_main(["--migrate-legacy", "account_a"])
        self.assertEqual(code, 0)
        self.assertEqual(migrate.call_args.args[2], "account_a")
        self.assertFalse(self.state_root.exists())

    def test_direct_sync_and_token_help_delegate_without_dependencies_or_credentials(self):
        for script in ("garmin/garmin_sync_coros.py", "coros/coros_sync_garmin.py",
                       "garmin/generate_garth_token.py", "garmin/generate_garth_token_browser.py"):
            result = subprocess.run([sys.executable, str(account_config.runtime_config.PROJECT_ROOT / "scripts" / script), "--help"],
                                    env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertIn("--account", result.stdout)

    def test_worker_shell_routes_both_directions_and_browser_modes_without_real_network(self):
        fake_python = self.root / "python"
        trace = self.root / "trace.jsonl"
        fake_python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "if sys.argv[1] == '-':\n"
            " sys.stdin.read(); sys.exit(0)\n"
            "if sys.argv[1] == '-m': sys.exit(0)\n"
            f"with open({str(trace)!r}, 'a') as output:\n"
            " output.write(json.dumps({'script': pathlib.Path(sys.argv[1]).name,\n"
            " 'email': os.environ['GARMIN_EMAIL'],\n"
            " 'headless': os.environ['GARMIN_BROWSER_HEADLESS'],\n"
            " 'id': os.environ['SYNC_ACCOUNT_ID']}) + '\\n')\n"
        )
        fake_python.chmod(0o700)
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        for direction, script in (("garmin_to_coros", "garmin_sync_coros.py"),
                                  ("coros_to_garmin", "coros_sync_garmin.py")):
            for mode in ([], ["--headless-only"], ["--browser-only"]):
                trace.unlink(missing_ok=True)
                env = account_config.worker_environment(profile, {"PATH": os.defpath}, self.state_root)
                env.update(PYTHON_BIN=str(fake_python), SYNC_DIRECTION=direction)
                result = subprocess.run(["bash", "run_sync.sh", *mode], env=env,
                                        cwd=account_config.runtime_config.PROJECT_ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual(calls[-1]["script"], script)
                self.assertEqual(calls[-1]["email"], profile.env["GARMIN_EMAIL"])
                self.assertEqual(calls[-1]["headless"], "1")
                if mode == ["--browser-only"]:
                    self.assertEqual(calls[0]["script"], "generate_garth_token_browser.py")
                    self.assertEqual(calls[0]["headless"], "0")

    def test_shell_refreshes_only_after_garmin_authentication_failure(self):
        fake_python = self.root / "python"
        trace = self.root / "calls.json"
        fake_python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            f"trace = pathlib.Path({str(trace)!r})\n"
            "calls = json.loads(trace.read_text()) if trace.exists() else []\n"
            "calls.append(pathlib.Path(sys.argv[1]).name)\n"
            "trace.write_text(json.dumps(calls))\n"
            "sys.exit(int(os.environ['TEST_SYNC_EXIT']) if len(calls) == 1 else 0)\n")
        fake_python.chmod(0o700)
        account = account_config.parse_config({"accounts": [account_data()]})[0]
        for first_exit, expected in ((1, ["garmin_sync_coros.py"]),
                                     (3, ["garmin_sync_coros.py", "generate_garth_token_browser.py", "garmin_sync_coros.py"])):
            trace.unlink(missing_ok=True)
            env = account_config.worker_environment(account, {"PATH": os.defpath}, self.state_root)
            env.update(PYTHON_BIN=str(fake_python), TEST_SYNC_EXIT=str(first_exit))
            result = subprocess.run(["bash", "run_sync.sh"], env=env,
                                    cwd=account_config.runtime_config.PROJECT_ROOT,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(trace.read_text()), expected)
            self.assertEqual(result.returncode, 1 if first_exit == 1 else 0)

    def test_redaction_covers_encoded_passwords_and_unknown_session_tokens(self):
        env = {"GARMIN_PASSWORD": "fake password+#", "HTTPS_PROXY": "http://user:proxy-pass@proxy.invalid"}
        for line in ("fake%20password%2B%23", "fake+password%2B%23", "Authorization: Bearer unknown-value",
                     '{"oauth1_token": "unknown-value"}', "proxy-pass"):
            result = sync_accounts.redact(line, env)
            self.assertNotIn("unknown-value", result)
            self.assertNotIn("proxy-pass", result)
            self.assertNotIn("password%2B", result)

    def test_terminal_progress_updates_one_line_and_preserves_group_prefix(self):
        output = TerminalOutput()
        lines = ["同步进度：0/2\n", "同步进度：1/2\n", "同步进度：2/2\n", "同步结果：成功 2 条，失败 0 条\n"]
        sync_accounts.forward_worker_output(lines, "account_b", {}, stream=output)
        self.assertEqual(output.getvalue().count("\r"), 3)
        self.assertEqual(output.getvalue().count("\n"), 2)
        self.assertIn("[account_b] 同步进度：2/2", output.getvalue())

    def test_non_terminal_progress_is_plain_text_and_always_redacted(self):
        output = io.StringIO()
        lines = ["同步进度：1/2 password=synthetic-private-value\n", "同步结果：成功 1 条\n"]
        sync_accounts.forward_worker_output(lines, "account_a", {}, stream=output)
        self.assertNotIn("\r", output.getvalue())
        self.assertNotIn("synthetic-private-value", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 2)

    def test_terminal_progress_ends_before_errors_and_on_worker_exit(self):
        output = TerminalOutput()
        sync_accounts.forward_worker_output(
            ["同步进度：0/2\n", "上传失败：RuntimeError\n", "同步进度：2/2\n"],
            "account_b", {}, stream=output,
        )
        self.assertIn("0/2\n[account_b] 上传失败", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\n"))

    def test_worker_uses_parent_terminal_mode_instead_of_inherited_flag(self):
        with patch.dict(os.environ, {"SYNC_PROGRESS_TTY": "1"}):
            self.assertEqual(self.run_main(["--account", "account_b"])[0], 0)
        state = json.loads((self.state_root / "account_b" / "worker.json").read_text())
        self.assertEqual(state["progress_tty"], "0")


if __name__ == "__main__":
    unittest.main()
