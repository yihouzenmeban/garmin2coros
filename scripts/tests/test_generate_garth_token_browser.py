from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from requests.exceptions import ConnectionError, HTTPError, SSLError, Timeout

from scripts.garmin import generate_garth_token_browser as browser_token
from scripts.garmin.generate_garth_token_browser import requires_manual_takeover


class FakeLocator:
    def __init__(self, visible=True, text=""):
        self._visible = visible
        self._text = text
        self.first = self

    def count(self):
        return 1

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text


class MissingLocator:
    first = None

    def count(self):
        return 0


class FakePage:
    def __init__(self, url="", content="", body_text="", locators=None):
        self.url = url
        self._content = content
        self._body_text = body_text
        self._locators = locators or {}

    def content(self):
        return self._content

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(text=self._body_text)
        return self._locators.get(selector, MissingLocator())


class RequiresManualTakeoverTest(unittest.TestCase):
    def test_login_uses_elapsed_time_including_page_operations_and_closes_browser(self):
        factory = MagicMock()
        browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://sso.garmin.com/sso/embed"
        page.is_closed.return_value = False
        now = [0.0]

        def slow_login(*args, **kwargs):
            now[0] += 4
            return False

        with patch.object(browser_token.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(browser_token, "attempt_auto_login", side_effect=slow_login), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                browser_token.capture_service_ticket("garmin.com", timeout_seconds=3,
                    playwright_factory=factory, login_email="test@example.invalid", login_password="test-password")
        browser.close.assert_called_once()
        page.wait_for_timeout.assert_not_called()
        self.assertLessEqual(page.goto.call_args.kwargs["timeout"], 3000)

    def test_navigation_failure_still_closes_browser(self):
        factory = MagicMock()
        browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.goto.side_effect = RuntimeError("synthetic-navigation-failure")
        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            browser_token.capture_service_ticket("garmin.com", playwright_factory=factory)
        browser.close.assert_called_once()

    def test_playwright_timeout_is_reported_as_a_login_timeout(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        factory = MagicMock()
        browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.goto.side_effect = PlaywrightTimeoutError("synthetic-private-value")
        with self.assertRaises(TimeoutError) as raised, redirect_stdout(io.StringIO()):
            browser_token.capture_service_ticket("garmin.com", playwright_factory=factory)
        self.assertNotIn("synthetic-private-value", str(raised.exception))
        browser.close.assert_called_once()

    def test_ignores_hidden_verify_text_before_login_form_renders(self):
        page = FakePage(
            url="https://sso.garmin.com/sso/embed",
            content="<html><script>const verifyToken = true;</script></html>",
            body_text="Sign in to Garmin Connect",
        )

        self.assertFalse(requires_manual_takeover(page))

    def test_detects_visible_verification_prompt(self):
        page = FakePage(
            url="https://sso.garmin.com/sso/challenge",
            body_text="Enter the verification code we sent to your email.",
        )

        self.assertTrue(requires_manual_takeover(page))


class OAuthConsumerTest(unittest.TestCase):
    def setUp(self):
        self.consumer = {
            "consumer_key": "test-consumer-key",
            "consumer_secret": "test-consumer-secret",
        }
        self.http_get = Mock(return_value=self.response())
        sleep_patch = patch.object(browser_token.time, "sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.factory = MagicMock()
        self.playwright = self.factory.return_value.__enter__.return_value
        self.browser = self.playwright.chromium.launch.return_value
        self.page = self.browser.new_page.return_value
        response = Mock(ok=True, status=200)
        response.json.return_value = self.consumer
        self.page.goto.return_value = response

    def response(self, status=200, payload=None):
        response = Mock(status_code=status)
        response.json.return_value = self.consumer if payload is None else payload
        if status >= 400:
            response.raise_for_status.side_effect = HTTPError(
                "test-proxy-password test-consumer-secret", response=response,
            )
        return response

    def fetch(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = browser_token.get_oauth_consumer(
                self.http_get, playwright_factory=self.factory,
            )
        self.assertNotIn("test-proxy-password", output.getvalue())
        self.assertNotIn("test-consumer-secret", output.getvalue())
        return result

    def test_success_validates_response_and_does_not_start_browser(self):
        self.assertEqual(self.fetch(), self.consumer)
        self.http_get.assert_called_once_with(browser_token.OAUTH_CONSUMER_URL, timeout=10)
        self.http_get.return_value.close.assert_called_once_with()
        self.factory.assert_not_called()
        self.sleep.assert_not_called()

    def test_transient_tls_connection_and_timeout_failures_are_retried(self):
        for error_class in (SSLError, ConnectionError, Timeout):
            with self.subTest(error=error_class.__name__):
                self.http_get.reset_mock()
                self.sleep.reset_mock()
                self.http_get.side_effect = [
                    error_class("test-proxy-password"), self.response(),
                ]

                self.assertEqual(self.fetch(), self.consumer)
                self.assertEqual(self.http_get.call_count, 2)
                self.sleep.assert_called_once_with(1)
                self.factory.assert_not_called()

    def test_retryable_http_status_is_retried(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                failed_response = self.response(status=status)
                self.http_get.reset_mock()
                self.sleep.reset_mock()
                self.http_get.side_effect = [failed_response, self.response()]

                self.assertEqual(self.fetch(), self.consumer)
                self.assertEqual(self.http_get.call_count, 2)
                failed_response.close.assert_called_once_with()
                self.factory.assert_not_called()

    def test_exhausted_tls_retries_use_headless_browser_with_group_proxy(self):
        self.http_get.side_effect = SSLError("test-proxy-password")
        with patch.dict(browser_token.os.environ, {
            "GARMIN_BROWSER_HEADLESS": "0",
            "HTTPS_PROXY": "http://127.0.0.1:17890",
        }, clear=True):
            self.assertEqual(self.fetch(), self.consumer)

        self.assertEqual(self.http_get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.playwright.chromium.launch.assert_called_once_with(
            headless=True, proxy={"server": "http://127.0.0.1:17890"},
        )
        self.browser.new_page.assert_called_once_with()
        self.page.goto.assert_called_once_with(
            browser_token.OAUTH_CONSUMER_URL, wait_until="domcontentloaded", timeout=10000,
        )
        self.browser.close.assert_called_once_with()

    def test_permanent_http_error_is_not_retried_or_leaked(self):
        self.http_get.return_value = self.response(status=403)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403") as raised:
            self.fetch()

        self.assertNotIn("test-proxy-password", str(raised.exception))
        self.assertNotIn("test-consumer-secret", str(raised.exception))
        self.http_get.assert_called_once()
        self.factory.assert_not_called()
        self.sleep.assert_not_called()

    def test_invalid_json_is_rejected_without_logging_response(self):
        self.http_get.return_value.json.side_effect = ValueError("test-consumer-secret")
        with self.assertRaisesRegex(RuntimeError, "JSON") as raised:
            self.fetch()

        self.assertNotIn("test-consumer-secret", str(raised.exception))
        self.http_get.assert_called_once()
        self.factory.assert_not_called()

    def test_missing_empty_or_wrong_type_consumer_fields_are_rejected(self):
        payloads = [
            [], {}, {"consumer_key": "test-consumer-key"},
            {"consumer_key": "", "consumer_secret": "test-consumer-secret"},
            {"consumer_key": "test-consumer-key", "consumer_secret": " "},
            {"consumer_key": 123, "consumer_secret": "test-consumer-secret"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.http_get.reset_mock()
                self.http_get.return_value = self.response(payload=payload)
                with self.assertRaisesRegex(RuntimeError, "OAuth 参数") as raised:
                    self.fetch()

                self.assertNotIn("test-consumer-secret", str(raised.exception))
                self.http_get.assert_called_once()
                self.factory.assert_not_called()

    def test_browser_network_failure_is_safe_and_always_closes_browser(self):
        self.http_get.side_effect = SSLError("test-proxy-password")
        self.page.goto.side_effect = RuntimeError("test-consumer-secret")
        with self.assertRaisesRegex(RuntimeError, "HTTPS_PROXY") as raised:
            self.fetch()

        self.assertNotIn("test-proxy-password", str(raised.exception))
        self.assertNotIn("test-consumer-secret", str(raised.exception))
        self.browser.close.assert_called_once_with()

    def test_browser_bad_http_status_missing_response_and_invalid_json_are_rejected(self):
        failed_http = Mock(ok=False, status=503)
        invalid_json = Mock(ok=True, status=200)
        invalid_json.json.side_effect = ValueError("test-consumer-secret")
        invalid_fields = Mock(ok=True, status=200)
        invalid_fields.json.return_value = {"consumer_key": "test-consumer-key"}
        for response in (failed_http, None, invalid_json, invalid_fields):
            with self.subTest(response=response):
                self.http_get.side_effect = SSLError("test-proxy-password")
                self.browser.close.reset_mock()
                self.page.goto.return_value = response
                with self.assertRaisesRegex(RuntimeError, "OAuth 参数") as raised:
                    self.fetch()

                self.assertNotIn("test-consumer-secret", str(raised.exception))
                self.browser.close.assert_called_once_with()


class BrowserTokenPreflightTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token_path = Path(directory.name) / "garth_token.enc"
        self.token_path.write_bytes(b"existing-test-encrypted-token")
        self.env = {
            "GARMIN_AUTH_DOMAIN": "COM", "GARMIN_EMAIL": "test@example.invalid",
            "GARMIN_PASSWORD": "test-password", "GARMIN_TOKEN_SALT": "test-salt",
        }
        self.runtime = {
            "http_get": Mock(), "oauth1_session_class": Mock(),
            "write_encrypted_token": Mock(), "token_path": self.token_path,
        }
        self.start_patch(patch.object(browser_token, "apply_runtime_env"))
        self.prepare = self.start_patch(patch.object(browser_token, "prepare_runtime", create=True))

    def test_browser_preparation_precedes_runtime_resolution_and_network_calls(self):
        events = []
        self.prepare.side_effect = lambda **kwargs: events.append("prepare")
        self.start_patch(patch.object(browser_token, "resolve_runtime",
                                     side_effect=lambda: events.append("resolve") or self.runtime))
        self.start_patch(patch.object(browser_token, "get_oauth_consumer",
                                     side_effect=RuntimeError("synthetic-stop")))
        with self.assertRaisesRegex(RuntimeError, "synthetic-stop"):
            browser_token.generate_token_via_browser(env=self.env)
        self.assertEqual(events, ["prepare", "resolve"])
        self.prepare.assert_called_once_with(browser_required=True)

    def test_preparation_failure_preserves_existing_token(self):
        self.prepare.side_effect = RuntimeError("synthetic-install-failure")
        resolve = self.start_patch(patch.object(browser_token, "resolve_runtime"))
        with self.assertRaisesRegex(RuntimeError, "synthetic-install-failure"):
            browser_token.generate_token_via_browser(env=self.env)
        resolve.assert_not_called()
        self.assertEqual(self.token_path.read_bytes(), b"existing-test-encrypted-token")

    def start_patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def test_consumer_download_failure_does_not_start_login_or_change_existing_token(self):
        self.start_patch(patch.object(
            browser_token, "get_oauth_consumer", side_effect=RuntimeError("OAuth 参数下载失败"),
        ))
        capture = self.start_patch(patch.object(browser_token, "capture_service_ticket"))

        with self.assertRaisesRegex(RuntimeError, "OAuth 参数下载失败"):
            browser_token.generate_token_via_browser(runtime=self.runtime, env=self.env)

        capture.assert_not_called()
        self.runtime["write_encrypted_token"].assert_not_called()
        self.assertEqual(self.token_path.read_bytes(), b"existing-test-encrypted-token")

    def test_consumer_is_ready_before_login_and_is_used_for_both_exchanges(self):
        consumer = {"consumer_key": "test-key", "consumer_secret": "test-secret"}
        events = []
        consumer_fetch = self.start_patch(patch.object(
            browser_token, "get_oauth_consumer",
            side_effect=lambda *args, **kwargs: events.append("consumer") or consumer,
        ))
        self.start_patch(patch.object(
            browser_token, "capture_service_ticket",
            side_effect=lambda **kwargs: events.append("login") or "test-ticket",
        ))
        oauth1 = self.start_patch(patch.object(
            browser_token, "get_oauth1_token", return_value={"oauth_token": "test-oauth1"},
        ))
        oauth2 = self.start_patch(patch.object(
            browser_token, "exchange_oauth2_token", return_value={"access_token": "test-oauth2"},
        ))
        self.start_patch(patch.object(browser_token, "fetch_username", return_value="test-user"))
        factory = Mock()

        result = browser_token.generate_token_via_browser(
            runtime=self.runtime, env=self.env, playwright_factory=factory,
        )

        self.assertEqual(events, ["consumer", "login"])
        consumer_fetch.assert_called_once_with(self.runtime["http_get"], playwright_factory=factory)
        self.assertEqual(oauth1.call_args.kwargs["consumer"], consumer)
        self.assertEqual(oauth2.call_args.kwargs["consumer"], consumer)
        self.runtime["write_encrypted_token"].assert_called_once()
        self.assertEqual(result["login_mode"], "browser")
        self.prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
