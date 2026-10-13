from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import account_config, account_state
from scripts.tests.test_account_config import account_data
from scripts.garmin.garth_token_store import write_encrypted_token


class AccountStateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = account_config.parse_config({"accounts": [account_data()]})[0]

    def test_binding_is_private_and_credential_rotation_keeps_progress(self):
        with account_state.lock_state(self.profile, self.root) as state:
            (state / "garmin.db").touch()
        metadata = (state / "binding.json").read_text()
        self.assertNotIn(self.profile.env["GARMIN_EMAIL"], metadata)
        self.assertNotIn(self.profile.env["GARMIN_PASSWORD"], metadata)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / "binding.json").stat().st_mode & 0o777, 0o600)
        changed = account_data()
        changed["garmin"]["password"] = "rotated-password"
        changed["garmin"]["token_salt"] = "rotated-salt"
        changed["direction"] = "coros_to_garmin"
        profile = account_config.parse_config({"accounts": [changed]})[0]
        with account_state.lock_state(profile, self.root):
            self.assertTrue((state / "garmin.db").exists())

    def test_reusing_populated_state_for_a_different_pair_or_region_is_rejected(self):
        with account_state.lock_state(self.profile, self.root) as state:
            self.create_database(state, rows=True)
        for provider, key, value in (("garmin", "email", "other@example.invalid"),
                                     ("coros", "email", "other@example.invalid"),
                                     ("garmin", "domain", "CN")):
            data = account_data()
            data[provider][key] = value
            profile = account_config.parse_config({"accounts": [data]})[0]
            with self.subTest(provider=provider, key=key), self.assertRaises(ValueError):
                with account_state.lock_state(profile, self.root):
                    self.fail("Must not reuse another account's state")

    def create_database(self, state, name="garmin.db", rows=False):
        table = "garmin_activity" if name == "garmin.db" else "coros_activity"
        with closing(sqlite3.connect(state / name)) as database:
            with database:
                database.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER)")
                if rows:
                    database.execute(f"INSERT INTO {table} (activity_id) VALUES (11)")

    def changed_profile(self):
        data = account_data()
        data["garmin"]["email"] = "changed@example.invalid"
        return account_config.parse_config({"accounts": [data]})[0]

    def test_unused_binding_and_empty_databases_can_rebind_without_changing_files(self):
        with account_state.lock_state(self.profile, self.root) as state:
            self.create_database(state)
            self.create_database(state, "coros.db")
            for directory in ("garmin-fit", "coros-fit"):
                (state / directory).mkdir()
        before = {path.name: path.read_bytes() for path in state.glob("*.db")}
        changed = self.changed_profile()
        with account_state.lock_state(changed, self.root):
            pass
        self.assertEqual(json.loads((state / "binding.json").read_text()), account_state.binding(changed))
        self.assertEqual((state / "binding.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual({path.name: path.read_bytes() for path in state.glob("*.db")}, before)

    def test_metadata_only_state_can_rebind_before_first_login(self):
        with account_state.lock_state(self.profile, self.root) as state:
            pass
        changed = self.changed_profile()
        with account_state.lock_state(changed, self.root):
            pass
        self.assertEqual(json.loads((state / "binding.json").read_text()), account_state.binding(changed))

    def test_unused_database_with_activity_index_can_rebind(self):
        with account_state.lock_state(self.profile, self.root) as state:
            self.create_database(state)
            with closing(sqlite3.connect(state / "garmin.db")) as database:
                database.execute("CREATE INDEX garmin_activity_activity_id ON garmin_activity(activity_id)")
        with account_state.lock_state(self.changed_profile(), self.root):
            pass

    def test_token_download_unknown_artifact_or_unreadable_database_blocks_rebinding(self):
        for artifact in ("garth_token.enc", "garmin-fit/11.zip", "coros-fit/11.fit", "unknown.json", "garmin.db"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with account_state.lock_state(self.profile, root) as state:
                    path = state / artifact
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(b"synthetic retained state")
                original = (state / "binding.json").read_bytes()
                with self.assertRaises(ValueError), account_state.lock_state(self.changed_profile(), root):
                    pass
                self.assertEqual((state / "binding.json").read_bytes(), original)
                self.assertEqual(path.read_bytes(), b"synthetic retained state")

    def test_deleted_activity_rows_still_count_as_used_state(self):
        with account_state.lock_state(self.profile, self.root) as state:
            self.create_database(state, rows=True)
            with closing(sqlite3.connect(state / "garmin.db")) as database:
                with database:
                    database.execute("DELETE FROM garmin_activity")
        with self.assertRaises(ValueError), account_state.lock_state(self.changed_profile(), self.root):
            pass

    def test_committed_wal_records_block_rebinding_even_before_checkpoint(self):
        with account_state.lock_state(self.profile, self.root) as state:
            self.create_database(state)
        with closing(sqlite3.connect(state / "garmin.db")) as database:
            database.execute("PRAGMA journal_mode=WAL")
            with database:
                database.execute("INSERT INTO garmin_activity (activity_id) VALUES (11)")
            with self.assertRaises(ValueError), account_state.lock_state(self.changed_profile(), self.root):
                pass

    def test_invalid_binding_or_unknown_database_schema_is_never_repaired_implicitly(self):
        for kind in ("invalid-json", "wrong-version", "wrong-id", "unknown-table", "lookalike-internal-table"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with account_state.lock_state(self.profile, root) as state:
                    self.create_database(state)
                metadata = account_state.binding(self.profile)
                if kind == "wrong-version":
                    metadata["version"] = 2
                elif kind == "wrong-id":
                    metadata["id"] = "another-group"
                elif kind == "unknown-table":
                    with closing(sqlite3.connect(state / "garmin.db")) as database:
                        with database:
                            database.execute("CREATE TABLE unknown (data TEXT)")
                elif kind == "lookalike-internal-table":
                    with closing(sqlite3.connect(state / "garmin.db")) as database:
                        with database:
                            database.execute("CREATE TABLE sqliteXhistory (data TEXT)")
                            database.execute("INSERT INTO sqliteXhistory VALUES ('synthetic retained history')")
                (state / "binding.json").write_text("invalid" if kind == "invalid-json" else json.dumps(metadata))
                original = (state / "binding.json").read_bytes()
                with self.assertRaises(ValueError), account_state.lock_state(self.changed_profile(), root):
                    pass
                self.assertEqual((state / "binding.json").read_bytes(), original)

    def test_failed_atomic_rebind_preserves_original_binding_and_removes_temp_file(self):
        with account_state.lock_state(self.profile, self.root) as state:
            pass
        original = (state / "binding.json").read_bytes()
        with patch.object(account_state.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError), account_state.lock_state(self.changed_profile(), self.root):
                pass
        self.assertEqual((state / "binding.json").read_bytes(), original)
        self.assertEqual(sorted(path.name for path in state.iterdir()), [".lock", "binding.json"])

    def test_unbound_existing_data_and_symlinked_directory_are_rejected(self):
        state = self.root / self.profile.id
        state.mkdir()
        (state / "garmin.db").touch()
        with self.assertRaises(ValueError), account_state.lock_state(self.profile, self.root):
            pass
        other = account_config.parse_config({"accounts": [account_data("other")]})[0]
        (self.root / "other").symlink_to(state, target_is_directory=True)
        with self.assertRaises(ValueError), account_state.lock_state(other, self.root):
            pass

    def test_same_group_cannot_run_concurrently_but_other_groups_can(self):
        other = account_config.parse_config({"accounts": [account_data("other")]})[0]
        with account_state.lock_state(self.profile, self.root):
            with self.assertRaises(ValueError), account_state.lock_state(self.profile, self.root):
                pass
            with account_state.lock_state(other, self.root):
                pass
        with account_state.lock_state(self.profile, self.root):
            pass

    def test_bound_state_cannot_link_to_another_groups_database(self):
        with account_state.lock_state(self.profile, self.root) as state:
            pass
        other = self.root / "other.db"
        other.touch()
        (state / "garmin.db").symlink_to(other)
        with self.assertRaises(ValueError), account_state.lock_state(self.profile, self.root):
            pass

    def legacy_database(self):
        source = self.root / "legacy"
        source.mkdir()
        with closing(sqlite3.connect(source / "garmin.db")) as connection:
            with connection:
                connection.execute("CREATE TABLE progress (activity_id INTEGER)")
                connection.execute("INSERT INTO progress VALUES (11)")
        write_encrypted_token(str(source / "garth_token.enc"), "synthetic-salt", "synthetic-token")
        return source

    def test_explicit_migration_copies_only_to_matching_group_without_changing_source(self):
        source = self.legacy_database()
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}
        target = account_state.migrate_legacy(self.profile, self.profile, self.root / "accounts", source)
        with closing(sqlite3.connect(target / "garmin.db")) as connection:
            self.assertEqual(connection.execute("SELECT * FROM progress").fetchall(), [(11,)])
        self.assertEqual((target / "garth_token.enc").read_bytes(), (source / "garth_token.enc").read_bytes())
        self.assertEqual((target / "garth_token.enc").stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()})
        self.assertEqual(json.loads((target / "binding.json").read_text()), account_state.binding(self.profile))
        with self.assertRaises(ValueError):
            account_state.migrate_legacy(self.profile, self.profile, self.root / "accounts", source)

    def test_migration_rejects_different_accounts_or_token_salt(self):
        source = self.legacy_database()
        for field, value in (("email", "other@example.invalid"), ("token_salt", "wrong-salt")):
            data = account_data()
            data["garmin"][field] = value
            profile = account_config.parse_config({"accounts": [data]})[0]
            with self.subTest(field=field), self.assertRaises(ValueError):
                account_state.migrate_legacy(profile, self.profile, self.root / "accounts", source)
        self.assertFalse((self.root / "accounts" / self.profile.id).exists())

    def test_migration_of_invalid_database_does_not_leave_partial_bound_state(self):
        source = self.legacy_database()
        (source / "coros.db").write_bytes(b"invalid sqlite database")
        with self.assertRaises(ValueError):
            account_state.migrate_legacy(self.profile, self.profile, self.root / "accounts", source)
        target = self.root / "accounts" / self.profile.id
        self.assertEqual([path.name for path in target.iterdir()], [".lock"])


if __name__ == "__main__":
    unittest.main()
