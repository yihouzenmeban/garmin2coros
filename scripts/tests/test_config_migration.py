import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest

from scripts import account_config, config_migration
from scripts.garmin.garth_token_store import write_encrypted_token
from scripts.tests.test_account_config import account_data


class ConfigMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env_file = self.root / ".env.garmin.local"
        self.config_file = self.root / "config.local.json"
        self.source = self.root / "db"
        self.source.mkdir()
        self.states = self.source / "accounts"
        self.backups = self.root / "backups"
        self.profile = account_config.parse_config({"accounts": [account_data()]})[0]
        values = {**self.profile.env, "GARMIN_BROWSER_HEADLESS": "0",
                  "HTTPS_PROXY": "", "HTTP_PROXY": "http://proxy.invalid"}
        self.env_file.write_text("\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()))
        self.original = self.env_file.read_bytes()
        with closing(sqlite3.connect(self.source / "garmin.db")) as database:
            with database:
                database.execute("CREATE TABLE progress (activity_id INTEGER)")
                database.execute("INSERT INTO progress VALUES (11)")
        write_encrypted_token(str(self.source / "garth_token.enc"), "synthetic-salt", "synthetic-token")

    def migrate(self):
        return config_migration.migrate_configuration(
            self.env_file, self.config_file, "account_a", self.states,
            self.source, self.backups,
        )

    def test_migration_preserves_credentials_parameters_progress_and_private_backup(self):
        original_db = hashlib.sha256((self.source / "garmin.db").read_bytes()).digest()
        backup = self.migrate()
        accounts = account_config.load_accounts(self.config_file, env={})
        for key in account_config.CREDENTIAL_KEYS:
            self.assertEqual(accounts[0].env[key], self.profile.env[key])
        self.assertEqual(accounts[0].env["GARMIN_BROWSER_HEADLESS"], "0")
        self.assertEqual(accounts[0].env["HTTP_PROXY"], "http://proxy.invalid")
        self.assertEqual(accounts[0].env["HTTPS_PROXY"], "")
        self.assertEqual(self.config_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.env_file.exists())
        self.assertTrue(backup.read_bytes() == self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(hashlib.sha256((self.source / "garmin.db").read_bytes()).digest(), original_db)
        with closing(sqlite3.connect(self.states / "account_a" / "garmin.db")) as database:
            self.assertEqual(database.execute("SELECT * FROM progress").fetchall(), [(11,)])
        self.assertTrue((self.states / "account_a" / "garth_token.enc").read_bytes()
                        == (self.source / "garth_token.enc").read_bytes())

    def test_existing_different_pair_is_not_overwritten(self):
        different = account_data()
        different["coros"]["email"] = "other@example.invalid"
        self.config_file.write_text(json.dumps({"accounts": [different]}))
        before = self.config_file.read_bytes()
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertTrue(self.config_file.read_bytes() == before)
        self.assertTrue(self.env_file.read_bytes() == self.original)
        self.assertFalse(self.states.exists())

    def test_failed_state_migration_does_not_retire_source_or_keep_partial_config(self):
        (self.source / "coros.db").write_bytes(b"invalid sqlite")
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertFalse(self.config_file.exists())
        self.assertTrue(self.env_file.read_bytes() == self.original)
        self.assertFalse(self.backups.exists())

    def test_config_only_migration_initializes_bound_state(self):
        for path in self.source.iterdir():
            path.unlink()
        self.migrate()
        self.assertTrue((self.states / "account_a" / "binding.json").exists())
        self.assertEqual(account_config.load_accounts(self.config_file, env={})[0].id, "account_a")

    def test_legacy_parser_preserves_shell_characters_without_execution_and_errors_are_safe(self):
        marker = self.root / "executed"
        self.env_file.write_text("GARMIN_PASSWORD=" + shlex.quote(f"$(touch {marker}) # quote'") + "\n")
        values = config_migration.read_legacy_values(self.env_file)
        self.assertEqual(values["GARMIN_PASSWORD"], f"$(touch {marker}) # quote'")
        self.assertFalse(marker.exists())
        self.env_file.write_text("GARMIN_PASSWORD='synthetic-private-value\n")
        with self.assertRaises(ValueError) as raised:
            config_migration.read_legacy_values(self.env_file)
        self.assertNotIn("synthetic-private-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
