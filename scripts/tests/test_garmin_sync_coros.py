from contextlib import closing, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import runpy
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from scripts import account_config

from scripts.garmin import garmin_sync_coros
from coros.coros_client import CorosActivityUploadError, CorosLoginError
from garmin import garmin_db
from garmin import garth_auth
import config
import sqlite_db
from oss.sts_token_error import StsTokenError


class GarminSyncCorosTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_dir = Path(directory.name)
        for module in (config, garmin_db, sqlite_db):
            self.start_patch(patch.object(module, "DB_DIR", str(self.database_dir)))
        self.start_patch(
            patch.object(config, "GARMIN_FIT_DIR", str(self.database_dir / "fit"))
        )
        self.start_patch(patch.dict(os.environ, {
            "SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a",
            "GARMIN_EMAIL": "test@example.invalid",
            "GARMIN_PASSWORD": "test-password",
            "GARMIN_TOKEN_SALT": "test-salt",
            "GARMIN_AUTH_DOMAIN": "COM",
            "COROS_EMAIL": "test@example.invalid",
            "COROS_PASSWORD": "test-password",
        }))
        self.database = garmin_sync_coros.GarminDB("garmin.db")
        self.database.initDB()
        self.garmin = Mock()
        self.garmin.getAllActivities.return_value = [{"activityId": 11}]
        self.garmin.downloadFitActivity.return_value = b"test activity archive"
        self.coros = Mock()
        self.coros.regionId = 2
        self.coros.userId = "test-user"
        self.coros.accessToken = "test-login-token"
        self.coros.uploadActivity.return_value = True
        self.storage = Mock()
        self.storage.bucket = "sts-selected-bucket"
        self.storage.multipart_upload.return_value = "fit_zip/test-user/test.zip"
        self.garmin_factory = self.start_patch(
            patch("garmin.garmin_client.GarminClient", return_value=self.garmin)
        )
        self.start_patch(
            patch("garmin.garmin_db.GarminDB", return_value=self.database)
        )
        self.coros_factory = self.start_patch(
            patch("coros.coros_client.CorosClient", return_value=self.coros)
        )
        self.ali_factory = self.start_patch(
            patch("oss.ali_oss_client.AliOssClient", return_value=self.storage)
        )
        self.aws_factory = self.start_patch(
            patch("oss.aws_oss_client.AwsOssClient", return_value=self.storage)
        )

    def start_patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def run_sync(self):
        output = io.StringIO()
        errors = io.StringIO()
        exit_code = 0
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                runpy.run_path(garmin_sync_coros.__file__, run_name="__main__")
            except SystemExit as exception:
                exit_code = exception.code or 0
        return exit_code, output.getvalue(), errors.getvalue()

    def sync_status(self, activity_id):
        with closing(sqlite3.connect(self.database_dir / "garmin.db")) as connection:
            return connection.execute(
                "SELECT is_sync_coros FROM garmin_activity WHERE activity_id = ?",
                (activity_id,),
            ).fetchone()[0]

    def test_success_marks_synced_and_reports_counts(self):
        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(self.sync_status(11), 1)
        self.assertIn("获取 Garmin 运动：1 条", output)
        self.assertIn("待同步：1 条", output)
        self.assertIn("同步结果：成功 1 条，失败 0 条", output)
        self.assertEqual(
            self.coros.uploadActivity.call_args.args[0],
            self.storage.multipart_upload.return_value,
        )
        self.assertEqual(
            self.coros.uploadActivity.call_args.kwargs["bucket"], self.storage.bucket
        )
        self.ali_factory.assert_called_once_with(
            bucket="coros-oss", service="aliyun",
            access_token="test-login-token", region_id=2,
        )

    def test_login_tls_failure_reports_safe_reason_and_preserves_pending_records(self):
        from requests import Request
        from requests.exceptions import SSLError

        self.database.saveActivity(11)
        self.garmin.ensure_login.side_effect = SSLError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] synthetic-private-value",
            request=Request("GET", "https://connectapi.garmin.com/private?token=synthetic-token").prepare(),
        )
        exit_code, output, errors = self.run_sync()
        self.assertEqual(exit_code, 1)
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", errors)
        self.assertIn("connectapi.garmin.com", errors)
        self.assertNotIn("synthetic-", errors)
        self.assertEqual(self.sync_status(11), 0)
        self.storage.multipart_upload.assert_not_called()

    def test_sync_consumes_selected_json_account_values(self):
        profile = account_config.parse_config({"accounts": [{
            "id": "account_a", "options": {"GARMIN_NEWEST_NUM": 6},
            "garmin": {"email": "local@example.invalid", "password": "local-test-password",
                       "token_salt": "local-test-salt", "domain": "CN"},
            "coros": {"email": "coros@example.invalid", "password": "coros-test-password"},
        }]})[0]
        with patch.dict(os.environ, account_config.worker_environment(profile, {}), clear=True):
            exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.garmin_factory.assert_called_once_with(
            "local@example.invalid", "local-test-password", "CN", "6",
            token_salt="local-test-salt",
        )
        self.coros_factory.assert_called_once_with(
            "coros@example.invalid", "coros-test-password",
        )

    def test_failed_records_are_retried_even_when_garmin_returns_no_new_records(self):
        self.database.saveActivity(11)
        self.database.updateExceptionSyncStatus(11)
        self.garmin.getAllActivities.return_value = []

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.sync_status(11), 1)
        self.garmin.downloadFitActivity.assert_called_once_with(11)

    def test_rejected_import_returns_failure_and_remains_retryable(self):
        self.coros.uploadActivity.return_value = False

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.sync_status(11), 2)
        self.assertEqual(self.database.getUnSyncActivity(), [11])
        self.assertIn("同步结果：成功 0 条，失败 1 条", output)

    def test_import_exception_returns_failure_and_can_succeed_on_the_next_run(self):
        self.coros.uploadActivity.side_effect = CorosActivityUploadError(
            "COROS activity import rejected: result=9999, status=None."
        )

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.sync_status(11), 2)
        self.assertIn("result=9999", errors)
        self.coros.uploadActivity.side_effect = None
        self.assertEqual(self.run_sync()[0], 0)
        self.assertEqual(self.sync_status(11), 1)

    def test_failed_download_returns_failure_and_is_not_uploaded(self):
        self.garmin.downloadFitActivity.side_effect = RuntimeError("download failed")

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.sync_status(11), 2)
        self.storage.multipart_upload.assert_not_called()
        self.coros.uploadActivity.assert_not_called()
        self.assertIn("下载", errors)

    def test_only_garmin_authentication_failures_request_browser_recovery(self):
        from utils.sync_errors import GarminAuthenticationError

        self.garmin.getAllActivities.side_effect = GarminAuthenticationError("Garmin 认证失效")
        self.assertEqual(self.run_sync()[0], 3)
        self.garmin.getAllActivities.side_effect = None
        self.garmin.downloadFitActivity.side_effect = GarminAuthenticationError("Garmin 认证失效")
        self.assertEqual(self.run_sync()[0], 3)
        self.assertEqual(self.sync_status(11), 2)

    def test_failed_storage_upload_does_not_import_or_mark_synced(self):
        self.storage.multipart_upload.side_effect = RuntimeError("upload failed")

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.sync_status(11), 2)
        self.coros.uploadActivity.assert_not_called()
        self.assertIn("上传", errors)

    def test_one_failed_activity_does_not_skip_the_other_pending_activities(self):
        self.garmin.getAllActivities.return_value = [
            {"activityId": 11}, {"activityId": 22}
        ]
        def import_activity(object_key, checksum, filename, size, **kwargs):
            if filename == "11.zip":
                raise CorosActivityUploadError("test import failure")
            return True

        self.coros.uploadActivity.side_effect = import_activity

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.sync_status(11), 2)
        self.assertEqual(self.sync_status(22), 1)
        self.assertIn("同步结果：成功 1 条，失败 1 条", output)

    def test_uploads_overlap_with_bounded_downloads_and_main_thread_database_writes(self):
        self.garmin.getAllActivities.return_value = [{"activityId": activity_id} for activity_id in (11, 22, 33, 44)]
        owner = threading.get_ident()
        barrier = threading.Barrier(2, timeout=1)
        lock = threading.Lock()
        upload_threads = set()
        downloaded = completed = active = peak = 0
        original_update = self.database.updateSyncStatus

        def download(activity_id):
            nonlocal downloaded
            self.assertNotEqual(threading.get_ident(), owner)
            with lock:
                downloaded += 1
                self.assertLessEqual(downloaded - completed, 2)
            return b"synthetic activity archive"

        def upload(file_path, object_name):
            nonlocal completed, active, peak
            with lock:
                upload_threads.add(threading.get_ident())
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
                completed += 1
            return f"fit_zip/{object_name}"

        def update(activity_id):
            self.assertEqual(threading.get_ident(), owner)
            original_update(activity_id)

        self.garmin.downloadFitActivity.side_effect = download
        self.storage.multipart_upload.side_effect = upload
        with patch.dict(os.environ, {"SYNC_UPLOAD_WORKERS": "2"}), \
             patch.object(self.database, "updateSyncStatus", side_effect=update):
            code, output, errors = self.run_sync()

        self.assertEqual(code, 0, errors)
        self.assertEqual(peak, 2)
        self.assertEqual(len(upload_threads), 2)
        self.assertNotIn(owner, upload_threads)
        self.assertEqual([self.sync_status(activity_id) for activity_id in (11, 22, 33, 44)], [1, 1, 1, 1])
        self.ali_factory.assert_called_once()

    def test_downloads_overlap_and_have_their_own_concurrency_limit(self):
        self.garmin.getAllActivities.return_value = [{"activityId": value} for value in range(1, 7)]
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=2)
        active = peak = 0

        def download(activity_id):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return b"synthetic-fit"

        self.garmin.downloadFitActivity.side_effect = download
        with patch.dict(os.environ, {"SYNC_UPLOAD_WORKERS": "4", "SYNC_DOWNLOAD_WORKERS": "2"}):
            code, output, errors = self.run_sync()
        self.assertEqual(code, 0, errors)
        self.assertEqual(peak, 2)
        self.garmin.prepare_downloads.assert_called_once()
        self.garmin.close_downloads.assert_called_once()
        self.assertIn("下载累计", output)
        self.assertIn("上传累计", output)

    def test_one_upload_worker_preserves_serial_processing(self):
        self.garmin.getAllActivities.return_value = [{"activityId": 11}, {"activityId": 22}]
        events = []
        self.garmin.downloadFitActivity.side_effect = lambda activity_id: events.append(f"download {activity_id}") or b"test-fit"
        self.coros.uploadActivity.side_effect = lambda object_key, checksum, filename, size, **kwargs: events.append(f"import {filename}") or True
        with patch.dict(os.environ, {"SYNC_UPLOAD_WORKERS": "1"}):
            self.assertEqual(self.run_sync()[0], 0)
        self.assertEqual(events, ["download 11", "import 11.zip", "download 22", "import 22.zip"])

    def test_default_upload_pool_has_four_workers(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict(os.environ, {"SYNC_ACCOUNT_WORKER": "1", "SYNC_ACCOUNT_ID": "account_a"}), \
             patch("concurrent.futures.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as pool:
            self.assertEqual(self.run_sync()[0], 0)
        pool.assert_called_once_with(max_workers=4)

    def test_completed_progress_includes_failed_import_and_committed_success(self):
        self.garmin.getAllActivities.return_value = [{"activityId": 11}, {"activityId": 22}]

        def upload(file_path, object_key):
            if Path(file_path).stem == "11":
                raise RuntimeError("synthetic-private-password")
            return object_key

        self.storage.multipart_upload.side_effect = upload
        code, output, errors = self.run_sync()
        self.assertEqual(code, 1)
        self.assertEqual([self.sync_status(activity_id) for activity_id in (11, 22)], [2, 1])
        self.assertIn("2/2 (100%) 成功 1 失败 1", output)
        self.assertNotRegex(output + errors, r"运动 \d+")
        self.assertIn("失败摘要：上传失败：RuntimeError（1 条）", errors)
        self.assertNotIn("synthetic-private-password", output + errors)

    def test_many_upload_failures_are_summarized_without_per_activity_log_spam(self):
        self.garmin.getAllActivities.return_value = [{"activityId": activity_id} for activity_id in range(1, 61)]
        self.storage.multipart_upload.side_effect = RuntimeError("synthetic-private-password")
        code, output, errors = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn("60/60 (100%) 成功 0 失败 60", output)
        self.assertLessEqual(len(output.splitlines()), 28)
        self.assertEqual(errors.count("失败摘要："), 1)
        self.assertNotIn("synthetic-private-password", output + errors)

    def test_empty_batch_reports_complete_progress_without_starting_upload_pool(self):
        self.garmin.getAllActivities.return_value = []
        with patch("concurrent.futures.ThreadPoolExecutor") as pool:
            code, output, errors = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("0/0 (100%)", output)
        pool.assert_not_called()

    def test_duplicate_pending_rows_are_uploaded_once_and_count_as_one_activity(self):
        with closing(sqlite3.connect(self.database_dir / "garmin.db")) as database:
            with database:
                database.executemany("INSERT INTO garmin_activity (activity_id) VALUES (?)", [(11,), (11,)])
        self.garmin.getAllActivities.return_value = []
        code, output, errors = self.run_sync()
        self.assertEqual(code, 0, errors)
        self.garmin.downloadFitActivity.assert_called_once_with(11)
        self.storage.multipart_upload.assert_called_once()
        self.coros.uploadActivity.assert_called_once()
        self.assertFalse(self.database.getUnSyncActivity())
        self.assertIn("1/1 (100%) 成功 1 失败 0", output)

    def test_no_pending_records_reports_zero_and_skips_coros_login(self):
        self.database.saveActivity(11)
        self.database.updateSyncStatus(11)

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 0)
        self.assertIn("待同步：0 条", output)
        self.coros.login.assert_not_called()
        self.ali_factory.assert_not_called()
        self.aws_factory.assert_not_called()
        self.assertIn("模块加载耗时：", output)

    def test_full_scan_checkpoint_enables_incremental_runs_and_manual_override(self):
        self.database.saveActivity(11)
        self.database.updateSyncStatus(11)
        with patch.dict(os.environ, {"GARMIN_NEWEST_NUM": "0"}):
            code, output, errors = self.run_sync()
            self.assertEqual(code, 0, errors)
            self.assertIn("扫描模式：完整扫描", output)
            self.assertTrue(self.garmin.getAllActivities.call_args.kwargs["full_scan"])
            self.assertTrue((self.database_dir / "garmin-scan.json").exists())
            code, output, errors = self.run_sync()
            self.assertEqual(code, 0, errors)
            self.assertIn("扫描模式：增量扫描", output)
            self.assertFalse(self.garmin.getAllActivities.call_args.kwargs["full_scan"])
            with patch.dict(os.environ, {"SYNC_FULL_SCAN": "1", "GARMIN_NEWEST_NUM": "6"}):
                code, output, errors = self.run_sync()
                self.assertEqual(code, 0, errors)
                self.assertIn("扫描模式：完整扫描", output)
                self.assertTrue(self.garmin.getAllActivities.call_args.kwargs["full_scan"])

    def test_limited_scan_logs_the_configured_window(self):
        with patch.dict(os.environ, {"GARMIN_NEWEST_NUM": "6"}):
            code, output, errors = self.run_sync()
        self.assertEqual(code, 0, errors)
        self.assertIn("扫描模式：限量增量扫描（最多 6 条）", output)

    def test_failed_scan_does_not_advance_full_scan_checkpoint(self):
        self.garmin.getAllActivities.return_value = None
        self.assertEqual(self.run_sync()[0], 1)
        self.assertFalse((self.database_dir / "garmin-scan.json").exists())

    def test_invalid_garmin_activity_response_is_not_reported_as_success(self):
        self.garmin.getAllActivities.return_value = None

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertIn("获取 Garmin", errors)

    def test_login_failure_returns_nonzero_without_logging_credentials(self):
        self.coros.login.side_effect = CorosLoginError("test-password")

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertIn("登录 COROS", errors)
        self.assertNotIn("test-password", output + errors)
        self.assertEqual(self.sync_status(11), 0)

    def test_aws_bucket_matches_the_coros_account_region(self):
        self.coros.regionId = 1

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 0)
        self.aws_factory.assert_called_once_with(
            bucket="coros-s3", service="aws",
            access_token="test-login-token", region_id=1,
        )
        self.ali_factory.assert_not_called()

    def test_sts_failure_reports_the_safe_http_error_and_remains_retryable(self):
        self.ali_factory.side_effect = StsTokenError("COROS STS request failed: HTTP 401.")

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertIn("HTTP 401", errors)
        self.assertEqual(self.sync_status(11), 2)
        self.coros.uploadActivity.assert_not_called()

    def test_oauth_consumer_failure_reports_actionable_error_without_syncing(self):
        self.garmin.getAllActivities.side_effect = garth_auth.GarthOAuthConsumerError(
            "TLS/网络连接异常",
        )

        exit_code, output, errors = self.run_sync()

        self.assertEqual(exit_code, 1)
        self.assertIn("获取 Garmin 运动", errors)
        self.assertIn("OAuth 参数", errors)
        self.assertIn("HTTPS_PROXY", errors)
        self.assertNotIn("test-password", output + errors)
        self.assertFalse(self.database.getUnSyncActivity())
        self.coros.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
