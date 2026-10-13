import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import account_config


def account_data(account_id="account_a"):
    return {
        "id": account_id,
        "garmin": {
            "email": f"{account_id}@example.invalid",
            "password": "synthetic-garmin-password",
            "token_salt": "synthetic-salt",
            "domain": "COM",
        },
        "coros": {
            "email": f"coros-{account_id}@example.invalid",
            "password": "synthetic-coros-password",
        },
    }


class AccountConfigTest(unittest.TestCase):
    def test_download_workers_default_override_validation_and_migration(self):
        data = account_data()
        self.assertEqual(account_config.parse_config({"accounts": [data]})[0].env["SYNC_DOWNLOAD_WORKERS"], "4")
        data["options"] = {"SYNC_DOWNLOAD_WORKERS": 2}
        profile = account_config.parse_config({"accounts": [data]})[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            account_config.initialize_config(path, seed=profile)
            self.assertEqual(account_config.load_accounts(path, env={})[0].env["SYNC_DOWNLOAD_WORKERS"], "2")
        for value in (0, 9, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                account_config.parse_config({"accounts": [account_data()], "defaults": {"SYNC_DOWNLOAD_WORKERS": value}})
    def test_upload_workers_default_and_group_override_are_isolated(self):
        first, second = account_data(), account_data("account_b")
        self.assertEqual(account_config.parse_config({"accounts": [first]})[0].env["SYNC_UPLOAD_WORKERS"], "4")
        second["options"] = {"SYNC_UPLOAD_WORKERS": 2}
        accounts = account_config.parse_config({
            "defaults": {"SYNC_UPLOAD_WORKERS": 6}, "accounts": [first, second],
        })
        self.assertEqual([account.env["SYNC_UPLOAD_WORKERS"] for account in accounts], ["6", "2"])
        environment = account_config.worker_environment(accounts[1], {"SYNC_UPLOAD_WORKERS": "8"})
        self.assertEqual(environment["SYNC_UPLOAD_WORKERS"], "2")

    def test_upload_workers_require_integer_between_one_and_eight(self):
        for value in (0, -1, 9, True, 1.5, "4", "synthetic-secret"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SYNC_UPLOAD_WORKERS") as raised:
                account_config.parse_config({"defaults": {"SYNC_UPLOAD_WORKERS": value}, "accounts": [account_data()]})
            self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_worker_cannot_inherit_another_progress_terminal_flag(self):
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        environment = account_config.worker_environment(profile, {"SYNC_PROGRESS_TTY": "1"})
        self.assertNotIn("SYNC_PROGRESS_TTY", environment)

    def test_upload_workers_survive_initialization_and_legacy_conversion_as_integer(self):
        from scripts.config_migration import legacy_profile

        data = account_data()
        data["options"] = {"SYNC_UPLOAD_WORKERS": 2}
        profile = account_config.parse_config({"accounts": [data]})[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            account_config.initialize_config(path, seed=profile)
            migrated = account_config.load_accounts(path, env={})[0]
        self.assertEqual(migrated.env["SYNC_UPLOAD_WORKERS"], "2")
        self.assertEqual(legacy_profile(profile.env, "account_a").env["SYNC_UPLOAD_WORKERS"], "2")

    def test_defaults_and_group_options_are_merged_without_credentials_in_repr(self):
        data = account_data()
        data["options"] = {"GARMIN_NEWEST_NUM": 8}
        accounts = account_config.parse_config({
            "defaults": {"GARMIN_NEWEST_NUM": 5}, "accounts": [data],
        })
        self.assertEqual(accounts[0].env["GARMIN_NEWEST_NUM"], "8")
        self.assertNotIn(data["garmin"]["password"], repr(accounts[0]))
        self.assertNotIn(data["garmin"]["email"], repr(accounts[0]))

    def test_existing_forward_configuration_matches_direction_free_configuration(self):
        data = account_data()
        expected = account_config.parse_config({"accounts": [data]})
        data["direction"] = "garmin_to_coros"
        self.assertEqual(account_config.parse_config({"accounts": [data]}), expected)

    def test_disabled_blank_group_is_valid_but_not_enabled(self):
        data = account_data()
        data["enabled"] = False
        data["garmin"] = {"email": "", "password": "", "token_salt": ""}
        data["coros"] = {"email": "", "password": ""}
        self.assertFalse(account_config.parse_config({"accounts": [data]})[0].enabled)

    def test_invalid_ids_duplicates_and_direction_are_rejected(self):
        for account_id in ("../escape", "bad/id", "", "a" * 65):
            with self.subTest(account_id=account_id), self.assertRaises(ValueError):
                account_config.parse_config({"accounts": [account_data(account_id)]})
        with self.assertRaises(ValueError):
            account_config.parse_config({"accounts": [account_data("A"), account_data("a")]})
        data = account_data()
        data["direction"] = "unknown"
        with self.assertRaises(ValueError):
            account_config.parse_config({"accounts": [data]})

    def test_credentials_cannot_be_inherited_from_parent_or_defaults(self):
        data = account_data()
        del data["garmin"]["password"]
        with patch.dict(os.environ, {"GARMIN_PASSWORD": "parent-password"}):
            with self.assertRaises(ValueError):
                account_config.parse_config({"accounts": [data]})
        with self.assertRaises(ValueError):
            account_config.parse_config({"defaults": {"GARMIN_PASSWORD": "parent-password"},
                                         "accounts": [account_data()]})

    def test_invalid_types_options_and_regions_are_rejected_without_values(self):
        base = {"accounts": [account_data()]}
        invalids = [{"accounts": []}, {"accounts": "synthetic-secret"}]
        for field, value in (("enabled", "false"), ("garmin", "synthetic-secret"),
                             ("options", {"GARMIN_NEWEST_NUM": -1}),
                             ("options", {"GARMIN_BROWSER_TIMEOUT_SECONDS": 0}),
                             ("options", {"GARMIN_LOGIN_MODE": "unknown"}),
                             ("options", {"GARMIN_EMAIL": "synthetic-secret"})):
            data = copy.deepcopy(base)
            data["accounts"][0][field] = value
            invalids.append(data)
        data = copy.deepcopy(base)
        data["accounts"][0]["garmin"]["domain"] = "synthetic-secret"
        invalids.append(data)
        for data in invalids:
            with self.subTest(data=data), self.assertRaises(ValueError) as raised:
                account_config.parse_config(data)
            self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_json_errors_do_not_echo_private_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"synthetic-secret": invalid}', encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                account_config.load_accounts(path, env={})
            self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_worker_environment_is_complete_and_removes_other_account_controls(self):
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        env = account_config.worker_environment(profile, {
            "PATH": "/test/bin", "GARMIN_EMAIL": "parent@example.invalid",
            "GARMIN_ENV_FILE": "/parent.env", "SYNC_ACCOUNTS_JSON": "private-json",
            "GARMIN_BROWSER_HEADLESS": "0", "GARMIN_NEWEST_NUM": "999",
        })
        self.assertEqual(env["PATH"], "/test/bin")
        self.assertEqual(env["GARMIN_EMAIL"], profile.env["GARMIN_EMAIL"])
        self.assertEqual(env["GARMIN_NEWEST_NUM"], "0")
        self.assertNotIn("GARMIN_ENV_FILE", env)
        self.assertNotIn("SYNC_ACCOUNTS_JSON", env)
        self.assertNotIn("GARMIN_BROWSER_HEADLESS", env)
        self.assertEqual(env["SYNC_ACCOUNT_WORKER"], "1")
        self.assertEqual(env["SYNC_ACCOUNT_ID"], "account_a")

    def test_config_import_scopes_all_paths_before_clients_import(self):
        with tempfile.TemporaryDirectory() as directory:
            for account_id in ("account_a", "account_b"):
                env = {**os.environ, "SYNC_ACCOUNT_ID": account_id,
                       "SYNC_STATE_ROOT": directory}
                output = subprocess.check_output([
                    sys.executable, "-c",
                    "from scripts import config; import json; "
                    "print(json.dumps([config.DB_DIR, config.GARTH_TOKEN_FILE, "
                    "config.GARMIN_FIT_DIR]))",
                ], env=env, text=True)
                paths = [Path(value) for value in json.loads(output)]
                self.assertEqual(paths[0], Path(directory) / account_id)
                self.assertTrue(all(path.is_relative_to(paths[0]) for path in paths[1:]))

    def test_worker_partial_browser_env_does_not_gain_other_account_values(self):
        runtime = account_config.runtime_config
        with patch.dict(os.environ, {"SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a",
                                     "GARMIN_EMAIL": "other@example.invalid"}, clear=True):
            values = runtime.load_config_values(env={})
            runtime.apply_runtime_env(env={})
            self.assertNotIn("GARMIN_EMAIL", values)
            self.assertNotIn("HTTPS_PROXY", os.environ)

    def test_init_writes_private_template_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            account_config.initialize_config(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            data = json.loads(path.read_text())
            for account in data["accounts"]:
                self.assertEqual(account["garmin"]["password"], "")
                self.assertEqual(account["coros"]["password"], "")
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                account_config.initialize_config(path)
            self.assertEqual(path.read_bytes(), original)

    def test_init_from_selected_account_preserves_pair_options_and_salt(self):
        data = account_data()
        data["options"] = {"GARMIN_BROWSER_HEADLESS": True}
        profile = account_config.parse_config({"accounts": [data]})[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            account_config.initialize_config(path, seed=profile)
            migrated = account_config.load_accounts(path, env={})
            for key in account_config.CREDENTIAL_KEYS:
                self.assertEqual(migrated[0].env[key], profile.env[key])
            self.assertEqual(migrated[0].env["GARMIN_BROWSER_HEADLESS"], "1")

    def test_inline_json_is_supported_and_explicit_file_has_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(json.dumps({"accounts": [account_data("file_group")]}))
            env = {"SYNC_ACCOUNTS_JSON": json.dumps({"accounts": [account_data("inline_group")]})}
            self.assertEqual(account_config.load_accounts(env=env)[0].id, "inline_group")
            self.assertEqual(account_config.load_accounts(path, env=env)[0].id, "file_group")

    def test_global_garth_sessions_cannot_override_group_login(self):
        profile = account_config.parse_config({"accounts": [account_data()]})[0]
        env = account_config.worker_environment(profile, {
            **os.environ, "GARTH_TOKEN": "synthetic-invalid-token", "GARTH_HOME": "/other-account",
            "garth_token": "synthetic-invalid-token", "garth_home": "/other-account",
        })
        self.assertFalse(any(key.upper() in {"GARTH_TOKEN", "GARTH_HOME"} for key in env))
        result = subprocess.run([
            sys.executable, "-c", "from scripts.garmin.garth_auth import load_garth; "
            "garth = load_garth(); assert garth.client.oauth1_token is None",
        ], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
