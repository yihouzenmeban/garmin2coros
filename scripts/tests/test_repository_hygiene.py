from pathlib import Path
import json
import subprocess
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RepositoryHygieneTest(unittest.TestCase):
    def is_ignored(self, path):
        return subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        ).returncode == 0

    def test_private_configuration_and_runtime_artifacts_are_ignored(self):
        for path in (
            ".env.garmin.local", ".env", ".env.production", "accounts.env",
            "config.local.json", "db/garmin.db", "db/garth_token.enc", "db/coros.db",
            "state.sqlite3", "state.sqlite3-wal", ".venv/bin/python", "venv/bin/python",
            "scripts/__pycache__/config.pyc", ".pytest_cache/results", "sync.log",
            "garmin-fit/activity.zip", "coros-fit/activity.fit",
            "db/accounts/account_a/binding.json", "db/accounts/account_a/garmin.db",
            "db/accounts/account_a/garth_token.enc", "db/accounts/account_b/coros-fit/activity.fit",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.is_ignored(path), f"Runtime/private file must be ignored: {path}")

    def test_shareable_template_and_source_are_not_ignored(self):
        for path in ("config.example.json", "scripts/config.py", "docs/configuration.md", "run_sync.sh"):
            with self.subTest(path=path):
                self.assertFalse(self.is_ignored(path), f"Shared file must remain trackable: {path}")

    def test_multi_account_template_contains_only_empty_credentials(self):
        data = json.loads((PROJECT_ROOT / "config.example.json").read_text())
        for group in data["accounts"]:
            for provider, fields in (("garmin", ("email", "password", "token_salt")),
                                     ("coros", ("email", "password"))):
                for field in fields:
                    self.assertEqual(group[provider][field], "")


if __name__ == "__main__":
    unittest.main()
