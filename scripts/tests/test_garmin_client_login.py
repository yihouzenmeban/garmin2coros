import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from requests.exceptions import SSLError, Timeout
from unittest.mock import Mock, PropertyMock, call, patch

from scripts.garmin import garmin_client


class GarminClientLoginTest(unittest.TestCase):
    def setUp(self):
        self.garth = Mock()
        self.garth.client.sess.headers = {}
        self.garth.client.dumps.return_value = "refreshed-test-token"
        self.garth.connectapi.return_value = [{"activityId": 42}]
        self.username = PropertyMock(
            side_effect=[RuntimeError("No active session"), "test-user"]
        )
        type(self.garth.client).username = self.username
        self.start_patch(patch.object(garmin_client, "garth", self.garth))
        self.start_patch(patch.dict(os.environ, {"GARMIN_LOGIN_MODE": ""}))
        self.has_token = self.start_patch(
            patch.object(garmin_client, "has_encrypted_token", return_value=True)
        )
        self.read_token = self.start_patch(
            patch.object(
                garmin_client, "read_encrypted_token", return_value="saved-test-token"
            )
        )
        self.write_token = self.start_patch(
            patch.object(garmin_client, "write_encrypted_token")
        )
        self.client = garmin_client.GarminClient(
            "test@example.invalid",
            "test-password",
            "COM",
            token_salt="test-salt",
            token_path="unused-test-token.enc",
        )

    def start_patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def test_restored_session_does_not_report_authentication_failure(self):
        with self.assertNoLogs(garmin_client.logger, level=logging.WARNING):
            activities = self.client.getActivities(start=0, limit=100)

        self.assertEqual(activities, [{"activityId": 42}])
        self.garth.client.loads.assert_called_once_with("saved-test-token")
        self.write_token.assert_called_once_with(
            "unused-test-token.enc", "test-salt", "refreshed-test-token"
        )
        self.garth.login.assert_not_called()

    def test_active_session_skips_saved_token_and_password_login(self):
        self.username.side_effect = None
        self.username.return_value = "test-user"

        self.client.ensure_login()

        self.has_token.assert_not_called()
        self.read_token.assert_not_called()
        self.garth.login.assert_not_called()

    def test_invalid_saved_token_warns_and_falls_back_to_password(self):
        self.username.side_effect = RuntimeError("Expired session")

        with self.assertLogs(garmin_client.logger, level=logging.WARNING) as logs:
            self.client.ensure_login()

        self.assertTrue(
            any("GARTH_TOKEN validation failed" in message for message in logs.output)
        )
        self.garth.login.assert_called_once_with(
            "test@example.invalid", "test-password"
        )

    def test_password_login_failure_propagates_to_caller(self):
        self.has_token.return_value = False
        self.garth.login.side_effect = garmin_client.GarminNoLoginException(
            "Password login failed"
        )

        with self.assertRaisesRegex(
            garmin_client.GarminNoLoginException, "Password login failed"
        ):
            self.client.getActivities(start=0, limit=100)

        self.garth.connectapi.assert_not_called()

    def test_browser_login_failure_propagates_to_caller(self):
        self.has_token.return_value = False
        with patch.dict(os.environ, {"GARMIN_LOGIN_MODE": "browser"}):
            with patch(
                "scripts.garmin.generate_garth_token_browser.generate_token_via_browser",
                side_effect=garmin_client.GarminNoLoginException("Browser login failed"),
            ):
                with self.assertRaisesRegex(
                    garmin_client.GarminNoLoginException, "Browser login failed"
                ):
                    self.client.getActivities(start=0, limit=100)

        self.garth.connectapi.assert_not_called()

    def test_parallel_downloads_reuse_independent_sessions_without_relogging(self):
        self.username.side_effect = None
        self.username.return_value = "test-user"
        barrier = threading.Barrier(2, timeout=2)
        clients = []

        def make_client():
            client = Mock()
            client.download.return_value = b"synthetic-fit"
            clients.append(client)
            return client

        self.garth.Client.side_effect = make_client
        self.client.prepare_downloads()

        def download():
            barrier.wait()
            return [self.client.downloadFitActivity(11), self.client.downloadFitActivity(22)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: download(), range(2))), [[b"synthetic-fit"] * 2] * 2)
        self.assertEqual(len(clients), 2)
        for client in clients:
            client.loads.assert_called_once_with("refreshed-test-token")
            self.assertEqual(client.download.call_count, 2)
        self.garth.login.assert_not_called()
        self.client.close_downloads()
        for client in clients:
            client.sess.close.assert_called_once()

    def test_saved_session_network_timeout_does_not_trigger_relogin(self):
        self.username.side_effect = RuntimeError("No active session")
        with patch.object(self.client, "_restore_session_from_token", side_effect=Timeout("synthetic-private-value")), \
             patch.object(self.client, "_login_with_browser") as browser, \
             patch("time.sleep") as sleep:
            with self.assertRaises(Timeout):
                self.client.ensure_login()
        self.assertEqual(sleep.call_args_list, [call(60), call(180)])
        self.garth.login.assert_not_called()
        browser.assert_not_called()

    def test_temporary_tls_failure_retries_saved_session_and_recovers(self):
        self.username.side_effect = RuntimeError("No active session")
        error = SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] synthetic-private-value")
        with patch.object(self.client, "_restore_session_from_token", side_effect=[error, True]) as restore, \
             patch.object(self.client, "_login_with_browser") as browser, \
             patch("time.sleep") as sleep, \
             self.assertLogs(garmin_client.logger, level=logging.WARNING) as logs:
            self.client.ensure_login()
        self.assertEqual(restore.call_count, 2)
        sleep.assert_called_once_with(60)
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", " ".join(logs.output))
        self.assertIn("2/3", " ".join(logs.output))
        self.assertNotIn("synthetic-private-value", " ".join(logs.output))
        self.garth.login.assert_not_called()
        browser.assert_not_called()

    def test_certificate_and_authentication_errors_are_not_network_retried(self):
        for error in (SSLError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
                      garmin_client.GarminAuthenticationError("expired")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.client, "_restore_session_from_token", side_effect=error), \
                 patch("time.sleep") as sleep:
                self.username.side_effect = RuntimeError("No active session")
                if isinstance(error, SSLError):
                    with self.assertRaises(SSLError):
                        self.client.ensure_login()
                else:
                    self.client.ensure_login()
                    self.garth.login.assert_called_once()
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
