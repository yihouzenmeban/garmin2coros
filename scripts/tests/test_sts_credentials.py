import base64
from http.cookies import SimpleCookie
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts.garmin import garmin_sync_coros
from oss import coros_sts_client
from oss.sts_token_error import StsTokenError


class CorosStsCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.credentials = {
            "AccessKeyId": "test-access-key",
            "AccessKeySecret": "test-access-secret",
            "SecurityToken": "test-session-token",
            "Region": "oss-cn-beijing",
            "Bucket": "coros-oss",
        }
        self.set_credentials(self.credentials)

    def set_response(self, payload, status=200, content_type="application/json"):
        self.http.request.return_value = SimpleNamespace(
            status=status,
            headers={"Content-Type": content_type},
            data=json.dumps(payload).encode(),
        )

    def set_credentials(self, credentials):
        encoded = base64.b64encode(json.dumps(credentials).encode()).decode()
        self.set_response({"code": 200, "data": {"credentials": encoded}})

    def fetch(self, **overrides):
        arguments = {
            "bucket": "coros-oss",
            "service": "aliyun",
            "access_token": "test-login-token",
            "region_id": 2,
        }
        arguments.update(overrides)
        return coros_sts_client.fetch_sts_credentials(self.http, **arguments)

    def test_fetches_credentials_using_the_training_hub_session(self):
        self.assertEqual(self.fetch(), self.credentials)
        call = self.http.request.call_args
        self.assertEqual(call.args, ("GET", "https://trainingcn.coros.com/api/proxy/oss/sts"))
        self.assertEqual(call.kwargs["fields"], {"bucket": "coros-oss", "service": "aliyun", "v": 2})
        cookies = SimpleCookie()
        cookies.load(call.kwargs["headers"]["Cookie"])
        self.assertEqual(cookies["CPL-coros-token"].value, "test-login-token")
        self.assertEqual(cookies["CPL-coros-region"].value, "2")
        self.assertIn("timeout", call.kwargs)

    def test_selects_the_training_hub_for_each_account_region(self):
        for region_id, hostname in (
            (1, "https://training.coros.com"),
            (2, "https://trainingcn.coros.com"),
            (3, "https://trainingeu.coros.com"),
        ):
            with self.subTest(region_id=region_id):
                self.fetch(region_id=region_id)
                self.assertEqual(
                    self.http.request.call_args.args[1], hostname + "/api/proxy/oss/sts"
                )

    def test_requires_a_login_token_before_requesting_credentials(self):
        with self.assertRaisesRegex(StsTokenError, "login"):
            self.fetch(access_token=None)
        self.http.request.assert_not_called()

    def test_rejects_an_unknown_account_region(self):
        with self.assertRaisesRegex(StsTokenError, "region"):
            self.fetch(region_id=999)
        self.http.request.assert_not_called()

    def test_http_failure_reports_status_without_parsing_html(self):
        self.http.request.return_value = SimpleNamespace(
            status=404, headers={"Content-Type": "text/html"}, data=b"<html>not found</html>"
        )
        with self.assertRaisesRegex(StsTokenError, "HTTP 404"):
            self.fetch()

    def test_html_with_http_200_reports_a_non_json_response(self):
        self.http.request.return_value = SimpleNamespace(
            status=200, headers={"Content-Type": "text/html"}, data=b"<html>private-test-marker</html>"
        )
        with self.assertRaisesRegex(StsTokenError, "JSON") as exception:
            self.fetch()
        self.assertNotIn("private-test-marker", str(exception.exception))

    def test_rejected_sts_response_does_not_echo_the_server_message(self):
        self.set_response({"code": 401, "msg": "private-test-marker"})
        with self.assertRaisesRegex(StsTokenError, "401") as exception:
            self.fetch()
        self.assertNotIn("private-test-marker", str(exception.exception))

    def test_requires_credentials_in_a_success_response(self):
        self.set_response({"code": 200, "data": {}})
        with self.assertRaisesRegex(StsTokenError, "credentials"):
            self.fetch()

    def test_invalid_encoded_credentials_are_reported_without_leaking_values(self):
        encoded = base64.b64encode(b"private-test-marker").decode()
        self.set_response({"code": 200, "data": {"credentials": encoded}})
        with self.assertRaisesRegex(StsTokenError, "credentials") as exception:
            self.fetch()
        self.assertNotIn("private-test-marker", str(exception.exception))
        self.assertNotIn(encoded, str(exception.exception))

    def test_validates_service_specific_credential_fields(self):
        self.credentials.pop("SecurityToken")
        self.set_credentials(self.credentials)
        with self.assertRaisesRegex(StsTokenError, "SecurityToken"):
            self.fetch()

    def test_accepts_the_current_aws_credential_fields(self):
        credentials = {
            "AccessKeyId": "test-access-key",
            "SecretAccessKey": "test-access-secret",
            "SessionToken": "test-session-token",
            "Region": "us-east-1",
            "Bucket": "coros-s3",
        }
        self.set_credentials(credentials)
        self.assertEqual(self.fetch(service="aws", bucket="coros-s3", region_id=1), credentials)

    def test_success_does_not_log_credentials(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.fetch()
        self.assertEqual(output.getvalue(), "")

    def test_transport_failure_is_wrapped_without_logging_the_request(self):
        self.http.request.side_effect = RuntimeError("private-test-marker")
        with self.assertRaisesRegex(StsTokenError, "RuntimeError") as exception:
            self.fetch()
        self.assertNotIn("private-test-marker", str(exception.exception))


if __name__ == "__main__":
    unittest.main()
