import ssl
import unittest

import requests
from urllib3.exceptions import MaxRetryError, SSLError

from scripts.utils import sync_errors


class SyncErrorsTest(unittest.TestCase):
    def test_tls_details_preserve_reason_and_host_without_request_secrets(self):
        request = requests.Request(
            "GET", "https://user:synthetic-password@connectapi.garmin.com/private-token?token=synthetic-token",
            headers={"Authorization": "Bearer synthetic-secret"},
        ).prepare()
        error = requests.exceptions.SSLError(
            MaxRetryError(None, request.url, SSLError(
                ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] synthetic-secret"))),
            request=request,
        )
        detail = sync_errors.network_error_detail(error)
        self.assertIn("SSLError", detail)
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", detail)
        self.assertIn("connectapi.garmin.com", detail)
        for private in ("synthetic-", "private-token", "Authorization", "user:"):
            self.assertNotIn(private, detail)
        self.assertTrue(sync_errors.is_transient_network_error(error))

    def test_only_transient_network_errors_can_retry(self):
        cases = (
            (requests.exceptions.Timeout("private"), True),
            (requests.exceptions.ConnectionError("private"), True),
            (requests.exceptions.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING]"), True),
            (requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED]"), False),
            (requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, "private")), False),
            (requests.exceptions.ConnectionError(
                ssl.SSLCertVerificationError(1, "private")), False),
            (requests.exceptions.SSLError("unknown TLS failure"), False),
            (requests.exceptions.HTTPError(response=requests.Response()), False),
            (sync_errors.GarminAuthenticationError("private"), False),
            (ValueError("private"), False),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                self.assertEqual(sync_errors.is_transient_network_error(error), expected)
                self.assertNotIn("private", sync_errors.network_error_detail(error))

    def test_untrusted_exception_text_is_not_logged_as_a_hostname(self):
        error = requests.exceptions.SSLError(
            "host=synthetic-secret; password=private", request=requests.Request(
                "GET", "https://synthetic-secret.example.invalid/?token=private").prepare(),
        )
        self.assertEqual(sync_errors.network_error_detail(error), "SSLError")
