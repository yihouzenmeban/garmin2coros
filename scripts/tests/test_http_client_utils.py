import unittest
from unittest.mock import patch

from scripts.utils import http_client_utils


class HttpClientUtilsTest(unittest.TestCase):
    def test_http_pool_keeps_capacity_for_upload_workers_and_certificate_verification(self):
        for proxy in ("", "http://proxy.example.invalid"):
            with self.subTest(proxy=bool(proxy)), \
                 patch.dict(http_client_utils.os.environ, {"HTTPS_PROXY": proxy}, clear=True), \
                 patch.object(http_client_utils.urllib3, "PoolManager") as direct, \
                 patch.object(http_client_utils.urllib3, "ProxyManager") as proxied:
                http_client_utils.build_http_client()
                factory = proxied if proxy else direct
                self.assertEqual(factory.call_args.kwargs["maxsize"], 8)
                self.assertEqual(factory.call_args.kwargs["cert_reqs"], "CERT_REQUIRED")
                timeout = factory.call_args.kwargs["timeout"]
                self.assertEqual(timeout.connect_timeout, 10)
                self.assertEqual(timeout.read_timeout, 30)
                retries = factory.call_args.kwargs["retries"]
                self.assertEqual(retries.total, 2)
                self.assertNotIn("POST", retries.allowed_methods)


if __name__ == "__main__":
    unittest.main()
