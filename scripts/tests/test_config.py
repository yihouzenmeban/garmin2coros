from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.garmin import generate_garth_token
import config


class RuntimeConfigTest(unittest.TestCase):
    def test_runtime_requires_account_worker_and_stable_id(self):
        for env in ({"GARMIN_EMAIL": "synthetic@example.invalid"},
                    {"SYNC_ACCOUNT_WORKER": "1"}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    config.load_config_values()

    def test_explicit_worker_values_are_not_merged_with_other_credentials(self):
        with patch.dict(os.environ, {"GARMIN_PASSWORD": "other-password"}, clear=True):
            values = config.load_config_values({"GARMIN_EMAIL": "synthetic@example.invalid"})
        self.assertTrue(values == {"GARMIN_EMAIL": "synthetic@example.invalid"},
                        "Other configuration values were unexpectedly merged")

    def test_worker_environment_supplies_sync_values_and_defaults(self):
        with patch.dict(os.environ, {
            "SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a",
            "GARMIN_EMAIL": "synthetic@example.invalid", "GARMIN_NEWEST_NUM": "6",
        }, clear=True):
            values = config.load_sync_config({"GARMIN_EMAIL": "", "GARMIN_NEWEST_NUM": 0})
        self.assertEqual(values["GARMIN_EMAIL"], "synthetic@example.invalid")
        self.assertEqual(values["GARMIN_NEWEST_NUM"], "6")

    def test_runtime_options_apply_without_exporting_account_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            config.apply_runtime_env({"HTTPS_PROXY": "http://proxy.invalid",
                                      "GARMIN_BROWSER_HEADLESS": "1",
                                      "GARMIN_EMAIL": "synthetic@example.invalid"})
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://proxy.invalid")
            self.assertNotIn("GARMIN_EMAIL", os.environ)

    def test_partial_browser_env_preserves_process_runtime_flags(self):
        with patch.dict(os.environ, {"GARMIN_BROWSER_HEADLESS": "1",
                                     "GARMIN_BROWSER_CI_MODE": "1"}, clear=True):
            config.apply_runtime_env({"GARMIN_EMAIL": "synthetic@example.invalid"})
            self.assertEqual(os.environ["GARMIN_BROWSER_HEADLESS"], "1")
            self.assertEqual(os.environ["GARMIN_BROWSER_CI_MODE"], "1")

    def test_explicit_runtime_flags_override_process_flags(self):
        with patch.dict(os.environ, {"GARMIN_BROWSER_HEADLESS": "1"}, clear=True):
            config.apply_runtime_env({"GARMIN_BROWSER_HEADLESS": "0"})
            self.assertEqual(os.environ["GARMIN_BROWSER_HEADLESS"], "0")

    def test_token_loader_uses_injected_account_values(self):
        values = generate_garth_token.load_garmin_env({
            "GARMIN_EMAIL": "synthetic@example.invalid", "GARMIN_PASSWORD": "fake-password",
            "GARMIN_TOKEN_SALT": "fake-salt",
        })
        self.assertEqual(values["GARMIN_PASSWORD"], "fake-password")

    def test_cli_dispatch_forwards_group_selection_before_client_imports(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["sync.py", "--account", "account_b"]), \
             patch.object(os, "execv", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit):
                config.dispatch_cli(direction="coros_to_garmin")
        arguments = execute.call_args.args[1]
        self.assertEqual(Path(arguments[1]).name, "sync_accounts.py")
        self.assertIn("coros_to_garmin", arguments)
        self.assertEqual(arguments[-2:], ["--account", "account_b"])

    def test_token_cli_dispatch_selects_token_operation_and_worker_does_not_recurse(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["token.py"]), \
             patch.object(os, "execv", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit):
                config.dispatch_cli(refresh_token="browser")
        self.assertIn("--refresh-token", execute.call_args.args[1])
        with patch.dict(os.environ, {"SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a"}, clear=True), \
             patch.object(os, "execv") as execute:
            config.dispatch_cli()
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
