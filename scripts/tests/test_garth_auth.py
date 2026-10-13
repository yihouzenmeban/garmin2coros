import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GarthTelemetryTest(unittest.TestCase):
    def run_isolated(self, code):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GARTH_")
        }
        environment["GARTH_TELEMETRY_ENABLED"] = "true"
        environment["GARTH_TELEMETRY_SEND_TO_LOGFIRE"] = "true"
        setup = textwrap.dedent("""
            from unittest.mock import Mock

            import logfire
            import requests

            logfire.configure = Mock()
            requests.Session.request = Mock(
                side_effect=AssertionError("Tests must not make network requests")
            )
        """)
        result = subprocess.run(
            [sys.executable, "-c", setup + textwrap.dedent(code)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_client_import_disables_telemetry_before_garth_initialization(self):
        self.run_isolated("""
            from scripts.garmin.garmin_client import GarminClient
            import garth

            assert garth.client.telemetry.enabled is False
            assert garth.client.telemetry.send_to_logfire is False
            logfire.configure.assert_not_called()
            requests.Session.request.assert_not_called()
        """)

    def test_token_generator_disables_telemetry_before_garth_initialization(self):
        self.run_isolated("""
            from scripts.garmin.generate_garth_token import resolve_runtime

            runtime = resolve_runtime()

            assert runtime["garth"].client.telemetry.enabled is False
            assert runtime["garth"].client.telemetry.send_to_logfire is False
            logfire.configure.assert_not_called()
            requests.Session.request.assert_not_called()
        """)

    def test_loader_disables_telemetry_on_an_existing_garth_client(self):
        self.run_isolated("""
            import os

            os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
            os.environ["GARTH_TELEMETRY_SEND_TO_LOGFIRE"] = "false"
            import garth
            from scripts.garmin.garth_auth import load_garth

            garth.configure(telemetry_enabled=True, telemetry_callback=Mock())
            loaded_garth = load_garth()

            assert loaded_garth is garth
            assert loaded_garth.client.telemetry.enabled is False
            assert loaded_garth.client.telemetry.send_to_logfire is False
            logfire.configure.assert_not_called()
            requests.Session.request.assert_not_called()
        """)


if __name__ == "__main__":
    unittest.main()
