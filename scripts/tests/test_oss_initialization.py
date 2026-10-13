from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oss import ali_oss_client, aws_oss_client
from oss.sts_token_error import StsTokenError


class OssInitializationTest(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.credentials = {
            "AccessKeyId": "test-access-key",
            "AccessKeySecret": "test-access-secret",
            "SecurityToken": "test-session-token",
            "Region": "oss-cn-shanghai",
            "Bucket": "sts-selected-bucket",
        }
        self.set_credentials(self.credentials)
        for module in (ali_oss_client, aws_oss_client):
            self.start_patch(patch.object(module, "build_http_client", return_value=self.http))
        self.ali_auth = self.start_patch(patch.object(ali_oss_client.oss2, "StsAuth"))
        self.ali_bucket = self.start_patch(patch.object(ali_oss_client.oss2, "Bucket"))
        self.aws_sdk = self.start_patch(patch.object(aws_oss_client.boto3, "client"))

    def start_patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def set_credentials(self, credentials):
        encoded = base64.b64encode(json.dumps(credentials).encode()).decode()
        self.http.request.return_value = SimpleNamespace(
            status=200,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"code": 200, "data": {"credentials": encoded}}).encode(),
        )

    def test_ali_client_uses_authenticated_sts_region_and_bucket(self):
        client = ali_oss_client.AliOssClient(access_token="test-login-token", region_id=2)

        self.assertEqual(client.bucket, "sts-selected-bucket")
        self.ali_auth.assert_called_once_with(
            "test-access-key", "test-access-secret", "test-session-token"
        )
        self.ali_bucket.assert_called_once_with(
            self.ali_auth.return_value,
            "https://oss-cn-shanghai.aliyuncs.com",
            "sts-selected-bucket",
            connect_timeout=30,
        )
        self.assertEqual(self.http.request.call_args.args[1], "https://trainingcn.coros.com/api/proxy/oss/sts")

    def test_ali_client_accepts_region_names_without_the_oss_prefix(self):
        self.credentials["Region"] = "cn-shanghai"
        self.set_credentials(self.credentials)

        ali_oss_client.AliOssClient(access_token="test-login-token", region_id=2)

        self.assertEqual(
            self.ali_bucket.call_args.args[1], "https://oss-cn-shanghai.aliyuncs.com"
        )

    def test_ali_upload_workers_reuse_private_clients_without_fetching_sts_again(self):
        def bucket_factory(*args, **kwargs):
            bucket = Mock()
            bucket.put_object_from_file.return_value = SimpleNamespace(status=200)
            bucket.init_multipart_upload.return_value = SimpleNamespace(status=200, upload_id="test-upload-id")
            bucket.upload_part.return_value = SimpleNamespace(etag="test-etag")
            bucket.complete_multipart_upload.return_value = SimpleNamespace(status=200)
            return bucket

        self.ali_bucket.side_effect = bucket_factory
        client = ali_oss_client.AliOssClient(access_token="test-login-token", region_id=2)
        barrier = threading.Barrier(2, timeout=2)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output):
            path = Path(directory) / "test.zip"
            path.write_bytes(b"synthetic-fit-archive")

            def upload():
                barrier.wait()
                return [client.multipart_upload(str(path), "test-user/test.zip") for _ in range(2)]

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(upload) for _ in range(2)]
                self.assertEqual([future.result() for future in futures], [["fit_zip/test-user/test.zip"] * 2] * 2)

        self.assertEqual(self.ali_bucket.call_count, 3)
        self.http.request.assert_called_once()
        self.assertEqual(output.getvalue(), "")

    def test_aws_client_uses_the_current_fields_and_server_selected_region(self):
        self.set_credentials({
            "AccessKeyId": "test-access-key",
            "SecretAccessKey": "test-access-secret",
            "SessionToken": "test-session-token",
            "Region": "us-west-2",
            "Bucket": "sts-selected-bucket",
        })

        client = aws_oss_client.AwsOssClient(
            bucket="coros-s3", access_token="test-login-token", region_id=1
        )

        self.assertEqual(client.bucket, "sts-selected-bucket")
        self.aws_sdk.assert_called_once_with(
            "s3",
            region_name="us-west-2",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-access-secret",
            aws_session_token="test-session-token",
            config=unittest.mock.ANY,
        )
        sdk_config = self.aws_sdk.call_args.kwargs["config"]
        self.assertEqual(sdk_config.connect_timeout, 10)
        self.assertEqual(sdk_config.read_timeout, 30)
        self.assertEqual(self.http.request.call_args.args[1], "https://training.coros.com/api/proxy/oss/sts")

    def test_both_clients_report_http_failure_without_decoding_html(self):
        self.http.request.return_value = SimpleNamespace(
            status=404, headers={"Content-Type": "text/html"}, data=b"<html>private-test-marker</html>"
        )
        for client_class in (ali_oss_client.AliOssClient, aws_oss_client.AwsOssClient):
            with self.subTest(client=client_class.__name__):
                with self.assertRaisesRegex(StsTokenError, "HTTP 404") as exception:
                    client_class(access_token="test-login-token", region_id=2)
                self.assertNotIn("private-test-marker", str(exception.exception))
        self.ali_bucket.assert_not_called()
        self.aws_sdk.assert_not_called()

    def test_both_clients_require_a_coros_session_before_network_access(self):
        for client_class in (ali_oss_client.AliOssClient, aws_oss_client.AwsOssClient):
            with self.subTest(client=client_class.__name__):
                with self.assertRaisesRegex(StsTokenError, "login"):
                    client_class()
        self.http.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
