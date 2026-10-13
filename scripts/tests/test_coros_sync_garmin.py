from contextlib import closing, redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.garmin import garmin_sync_coros
from coros import coros_client, coros_db
import config
import sqlite_db


class CorosSyncGarminTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fit = self.root / "fit"
        for module in (config, coros_db, sqlite_db):
            self.start_patch(patch.object(module, "DB_DIR", str(self.root)))
        self.start_patch(patch.object(config, "COROS_FIT_DIR", str(self.fit)))
        self.start_patch(patch.dict(os.environ, {"SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a"}, clear=True))
        self.start_patch(patch.dict(sys.modules, {"coros_client": coros_client, "coros_db": coros_db}))
        self.database = coros_db.CorosDB("coros.db")
        self.database.initDB()
        self.coros = Mock()
        self.coros.getAllActivities.return_value = [{"labelId": 11, "sportType": 1}]
        self.coros.downloadActivitie.return_value.data = b"synthetic-fit"
        self.garmin = Mock()
        self.garmin.upload_activity.return_value = "SUCCESS"
        self.start_patch(patch.object(coros_client, "CorosClient", return_value=self.coros))
        self.start_patch(patch.object(coros_db, "CorosDB", return_value=self.database))
        self.garmin_factory = self.start_patch(patch("garmin.garmin_client.GarminClient", return_value=self.garmin))
        self.script = Path(garmin_sync_coros.__file__).parents[1] / "coros" / "coros_sync_garmin.py"

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_sync(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                runpy.run_path(str(self.script), run_name="__main__")
            except SystemExit as error:
                return error.code or 0, output.getvalue()
        return 0, output.getvalue()

    def status(self, activity_id):
        with closing(sqlite3.connect(self.root / "coros.db")) as database:
            return database.execute("SELECT is_sync_garmin FROM coros_activity WHERE activity_id = ?",
                                    (activity_id,)).fetchone()[0]

    def test_success_and_duplicate_uploads_return_success(self):
        self.garmin.upload_activity.return_value = "DUPLICATE_ACTIVITY"
        code, output = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.status(11), 1)
        self.assertIn("成功 1 条，失败 0 条", output)
        self.assertIn("1/1 (100%) 成功 1 失败 0", output)
        self.assertIn("模块加载耗时：", output)
        self.assertIn("扫描模式：完整扫描", output)

    def test_empty_batch_skips_garmin_initialization_and_logs_incremental_scan(self):
        self.database.saveActivity(11, 1)
        self.database.updateSyncStatus(11)
        self.assertEqual(self.run_sync()[0], 0)
        code, output = self.run_sync()
        self.assertEqual(code, 0, output)
        self.garmin_factory.assert_not_called()
        self.assertIn("扫描模式：增量扫描", output)
        self.assertIn("0/0 (100%)", output)

    def test_coros_batch_insert_preserves_status_and_scan_checkpoint(self):
        self.database.saveActivity(11, 1)
        self.database.updateSyncStatus(11)
        self.database.saveActivities([{"labelId": 11, "sportType": 1}, {"labelId": 22, "sportType": 2}, {"labelId": 22, "sportType": 2}])
        self.assertEqual(self.database.getKnownActivityIds(), {11, 22})
        self.assertEqual(self.database.getUnSyncActivity(), [{"id": 22, "sportType": 2}])
        self.assertEqual(self.run_sync()[0], 0)
        self.assertTrue(self.coros.getAllActivities.call_args.kwargs["full_scan"])
        self.assertEqual(self.run_sync()[0], 0)
        self.assertFalse(self.coros.getAllActivities.call_args.kwargs["full_scan"])

    def test_failed_upload_is_nonzero_retryable_and_does_not_skip_other_activities(self):
        self.coros.getAllActivities.return_value.append({"labelId": 22, "sportType": 2})
        self.garmin.upload_activity.side_effect = [ValueError("synthetic-private-password"), "SUCCESS"]
        code, output = self.run_sync()
        self.assertEqual(code, 1)
        self.assertEqual(self.status(11), 2)
        self.assertEqual(self.status(22), 1)
        self.assertIn("2/2 (100%) 成功 1 失败 1", output)
        self.assertNotRegex(output, r"运动 \d+")
        self.assertNotIn("synthetic-private-password", output)
        self.assertEqual(self.database.getUnSyncActivity(), [{"id": 11, "sportType": 1}])
        self.garmin.upload_activity.side_effect = None
        self.coros.getAllActivities.return_value = []
        self.assertEqual(self.run_sync()[0], 0)
        self.assertEqual(self.status(11), 1)

    def test_rejected_status_and_invalid_activity_response_return_nonzero(self):
        self.garmin.upload_activity.return_value = "FAILURE"
        self.assertEqual(self.run_sync()[0], 1)
        self.assertEqual(self.status(11), 2)
        self.coros.getAllActivities.return_value = None
        self.assertEqual(self.run_sync()[0], 1)

    def test_fetch_error_is_safe_and_does_not_return_success(self):
        self.coros.getAllActivities.side_effect = ValueError("synthetic-private-password")
        code, output = self.run_sync()
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-private-password", output)

    def test_garmin_login_tls_failure_reports_safe_reason(self):
        from requests.exceptions import SSLError

        self.garmin.ensure_login.side_effect = SSLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] synthetic-private-password")
        code, output = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", output)
        self.assertNotIn("synthetic-private-password", output)
        self.assertEqual(self.status(11), 0)
        self.garmin.upload_activity.assert_not_called()

    def test_coros_request_failure_is_an_exception_and_garmin_auth_is_distinct(self):
        client = garmin_sync_coros.CorosClient("test@example.invalid", "synthetic-password")
        client.accessToken = "synthetic-access-token"
        client.teamapi = "https://test.example.invalid"
        client.req = Mock()
        client.req.request.side_effect = TimeoutError("synthetic-private-password")
        for operation in (lambda: client.getActivities(200, 1), lambda: client.downloadActivitie(11, 1)):
            with self.assertRaises(Exception) as raised:
                operation()
            self.assertNotIn("synthetic-private-password", str(raised.exception))
        from utils.sync_errors import GarminAuthenticationError
        self.garmin.upload_activity.side_effect = GarminAuthenticationError("Garmin 认证失效")
        self.assertEqual(self.run_sync()[0], 3)


if __name__ == "__main__":
    unittest.main()
