from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.garmin import garmin_sync_coros
from coros.coros_client import CorosActivityUploadError
from garmin.garmin_client import GarminClient
from oss.ali_oss_client import AliOssClient
from oss.aws_oss_client import AwsOssClient


class AliActivityUploadTest(unittest.TestCase):
    def setUp(self):
        self.client = AliOssClient.__new__(AliOssClient)
        self.bucket = Mock()
        self.bucket.put_object_from_file.return_value.status = 200
        self.bucket.init_multipart_upload.return_value = SimpleNamespace(status=200, upload_id="test-upload")
        self.bucket.upload_part.return_value = SimpleNamespace(status=200, etag="test-etag")
        self.bucket.complete_multipart_upload.return_value.status = 200
        self.client._upload_client = Mock(return_value=self.bucket)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.zip"
        self.path.write_bytes(b"synthetic-fit")

    def test_small_files_use_one_request_and_preserve_the_object_key(self):
        self.assertEqual(self.client.multipart_upload(str(self.path), "user/test.zip"), "fit_zip/user/test.zip")
        self.bucket.put_object_from_file.assert_called_once_with("fit_zip/user/test.zip", str(self.path))

    def test_small_upload_failure_propagates(self):
        self.bucket.put_object_from_file.return_value.status = 503
        with self.assertRaises(RuntimeError):
            self.client.multipart_upload(str(self.path), "user/test.zip")

    def test_large_files_use_multipart_and_abort_failed_parts(self):
        with self.path.open("wb") as output:
            output.truncate(6 * 1024 * 1024)
        self.assertEqual(self.client.multipart_upload(str(self.path), "user/test.zip"), "fit_zip/user/test.zip")
        self.assertGreater(self.bucket.upload_part.call_count, 1)
        self.bucket.complete_multipart_upload.assert_called_once()
        self.bucket.upload_part.side_effect = RuntimeError("synthetic-failure")
        with self.assertRaises(RuntimeError):
            self.client.multipart_upload(str(self.path), "user/test.zip")
        self.bucket.abort_multipart_upload.assert_called_once_with("fit_zip/user/test.zip", "test-upload")


class CorosActivityUploadTest(unittest.TestCase):
    def setUp(self):
        self.client = garmin_sync_coros.CorosClient(
            "test@example.invalid", "test-password"
        )
        self.client.accessToken = "test-access-token"
        self.client.regionId = 2
        self.client.teamapi = "https://test.example.invalid"
        self.client.req = Mock()
        self.set_response({"result": "0000", "data": {"status": 2}})

    def set_response(self, payload, status=200):
        self.client.req.request.return_value = SimpleNamespace(
            status=status, data=json.dumps(payload).encode()
        )

    def upload(self):
        return self.client.uploadActivity(
            "fit_zip/test-user/test.zip", "test-md5", "test.zip", 10
        )

    def test_accepted_import_returns_success_without_dumping_response_data(self):
        self.set_response(
            {"result": "0000", "data": {"status": 2, "token": "private-test-marker"}}
        )
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertTrue(self.upload())

        self.assertEqual(output.getvalue(), "")

    def test_rejected_import_raises_with_result_and_status(self):
        self.set_response({"result": "0000", "data": {"status": 1}})

        with self.assertRaisesRegex(CorosActivityUploadError, "status=1"):
            self.upload()

    def test_error_response_without_data_raises_instead_of_exiting(self):
        self.set_response({"result": "9999", "data": None})

        with self.assertRaisesRegex(CorosActivityUploadError, "result=9999"):
            self.upload()

    def test_http_failure_is_not_treated_as_an_accepted_import(self):
        self.set_response({"result": "0000", "data": {"status": 2}}, status=503)

        with self.assertRaisesRegex(CorosActivityUploadError, "HTTP 503"):
            self.upload()

    def test_malformed_response_raises_instead_of_exiting(self):
        self.client.req.request.return_value = SimpleNamespace(
            status=200, data=b"not JSON"
        )

        with self.assertRaises(CorosActivityUploadError):
            self.upload()

    def test_network_failure_raises_instead_of_exiting(self):
        self.client.req.request.side_effect = ConnectionError("test network failure")

        with self.assertRaises(CorosActivityUploadError):
            self.upload()

    def test_import_uses_the_actual_uploaded_bucket(self):
        self.client.uploadActivity(
            "fit_zip/test-user/test.zip", "test-md5", "test.zip", 10,
            bucket="sts-selected-bucket",
        )

        parameter = self.client.req.request.call_args.kwargs["fields"]["jsonParameter"]
        self.assertEqual(json.loads(parameter)["bucket"], "sts-selected-bucket")


class AwsActivityUploadTest(unittest.TestCase):
    def setUp(self):
        self.client = AwsOssClient.__new__(AwsOssClient)
        self.client.bucket = "test-bucket"
        self.client.client = Mock()

    def test_successful_upload_returns_the_uploaded_object_key(self):
        object_key = self.client.multipart_upload("test.zip", "test-user/test.zip")

        self.assertEqual(object_key, "fit_zip/test-user/test.zip")
        self.assertEqual(
            self.client.client.upload_file.call_args.kwargs["Bucket"], "test-bucket"
        )
        self.assertEqual(
            self.client.client.upload_file.call_args.kwargs["Key"], object_key
        )

    def test_failed_upload_propagates_to_the_sync_pipeline(self):
        self.client.client.upload_file.side_effect = RuntimeError("test upload failure")

        with self.assertRaisesRegex(RuntimeError, "test upload failure"):
            self.client.multipart_upload("test.zip", "test-user/test.zip")

    def test_file_upload_does_not_expand_the_outer_pool_with_nested_workers(self):
        self.client.multipart_upload("test.zip", "test-user/test.zip")
        transfer = self.client.client.upload_file.call_args.kwargs["Config"]
        self.assertFalse(transfer.use_threads)
        self.assertEqual(transfer.max_concurrency, 1)


class GarminActivityUploadTest(unittest.TestCase):
    def test_garmin_upload_reuses_session_and_sets_timeout(self):
        client = GarminClient.__new__(GarminClient)
        client.ensure_login = Mock()
        client.garthClient = Mock()
        client.garthClient.client.domain = "garmin.com"
        client.headers = {}
        client.upload_session = Mock()
        client.upload_session.post.return_value = Mock(status_code=202)
        client.upload_session.post.return_value.json.return_value = {"detailedImportResult": {"uploadId": 11}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.fit"
            path.write_bytes(b"synthetic-fit")
            self.assertEqual(client.upload_activity(str(path)), "SUCCESS")
            self.assertEqual(client.upload_activity(str(path)), "SUCCESS")
        self.assertEqual(client.upload_session.post.call_count, 2)
        self.assertEqual(client.upload_session.post.call_args.kwargs["timeout"], (10, 30))

    def test_failed_request_returns_retryable_status_without_printing_private_error(self):
        client = GarminClient.__new__(GarminClient)
        client.ensure_login = Mock()
        client.garthClient = Mock()
        client.garthClient.client.domain = "garmin.com"
        client.headers = {}
        client.upload_session = Mock()
        client.upload_session.post.side_effect = ValueError("synthetic-private-password")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             redirect_stdout(output):
            path = Path(directory) / "test.fit"
            path.write_bytes(b"synthetic-fit")
            self.assertEqual(client.upload_activity(str(path)), "UPLOAD_EXCEPTION")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
