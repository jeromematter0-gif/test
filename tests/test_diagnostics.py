import contextlib
import io
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from riven_sniper.__main__ import main
from riven_sniper.core import (
    ApiFailure, MarketClient, Monitor, Settings, connection_failure,
    safe_failure_details, verified_ssl_context,
)
from riven_sniper.diagnostics import connection_report


class CertificateDiagnosticsTests(unittest.TestCase):
    def test_system_and_bundle_trust_keep_hostname_and_certificate_verification(self):
        context = verified_ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_real_certificate_cause_is_identified_without_leaking_error_text(self):
        certificate = ssl.SSLCertVerificationError(1, "secret@proxy.invalid")
        certificate.verify_code = 20
        error = httpx.ConnectError("secret@proxy.invalid")
        error.__cause__ = certificate
        failure = connection_failure(error, 65.5)
        self.assertEqual(failure.kind, "tls")
        self.assertTrue(failure.blocked)
        self.assertEqual(failure.duration_ms, 65.5)
        details = safe_failure_details(error)
        self.assertEqual(details["certificate_verify_codes"], [20])
        self.assertIn("SSLCertVerificationError", details["exception_types"])
        self.assertNotIn("secret", str(failure) + json.dumps(details))

    def test_dns_and_connection_refused_are_distinguished_from_tls(self):
        for cause, expected in [(socket.gaierror(-2, "secret"), "dns"),
                                (ConnectionRefusedError(10061, "secret"), "connection_refused")]:
            with self.subTest(kind=expected):
                error = httpx.ConnectError("secret")
                error.__cause__ = cause
                failure = connection_failure(error, 2)
                self.assertEqual(failure.kind, expected)
                self.assertNotIn("secret", str(failure))
                self.assertEqual(safe_failure_details(error)["network_codes"], [cause.errno])

    def test_timeout_preserves_failure_type_without_exposing_url(self):
        failure = connection_failure(httpx.ConnectTimeout("https://user:secret@proxy.invalid"), 15000)
        self.assertEqual(failure.kind, "timeout")
        self.assertIn("ConnectTimeout", str(failure))
        self.assertNotIn("secret", str(failure))


class FailureReportingTests(unittest.TestCase):
    def test_diagnostic_attempt_preserves_failure_identifiers_and_closes_client(self):
        requests = []

        def handler(request):
            requests.append(request)
            error = httpx.ConnectError("user:secret@proxy.invalid")
            error.__cause__ = socket.gaierror(-2, "private network name")
            raise error

        client = MarketClient(Settings(), transport=httpx.MockTransport(handler))
        report = connection_report(Settings(), client=client)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error_kind"], "dns")
        self.assertEqual(report["details"]["network_codes"], [-2])
        self.assertEqual(len(requests), 1)
        self.assertTrue(client.client.is_closed)
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("private network name", json.dumps(report))

    def test_report_distinguishes_valid_empty_snapshot_from_connection_failure(self):
        client = MarketClient(Settings(), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"payload": {"auctions": []}})))
        report = connection_report(Settings(), client=client)
        self.assertTrue(report["ok"])
        self.assertEqual(report["returned_count"], 0)
        self.assertNotIn("error_kind", report)

    def test_diagnose_cli_writes_report_exits_failure_and_does_not_start_server(self):
        report = {"ok": False, "error_kind": "dns", "message": "DNS impossible"}
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["riven_sniper", "--diagnose", "--data-dir", directory]), \
                 patch("riven_sniper.diagnostics.connection_report", return_value=report), \
                 patch("riven_sniper.__main__.create_server") as create_server, \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(json.loads((Path(directory) / "diagnostic.json").read_text()), report)
            create_server.assert_not_called()

    def test_twenty_three_failed_attempts_never_become_a_successful_monitor(self):
        class Client:
            def search(self):
                raise ApiFailure("Connexion impossible", kind="network", duration_ms=65)

            def close(self):
                pass

        class SimulatedWait:
            waits = []

            def is_set(self):
                return len(self.waits) >= 23

            def wait(self, delay=None):
                self.waits.append(delay)
                return self.is_set()

            def set(self):
                pass

        monitor = Monitor(Settings(), ":memory:", client=Client())
        self.addCleanup(monitor.close)
        monitor.stop_event = SimulatedWait()
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        status = monitor.state()["status"]
        self.assertEqual(status["polls"], 23)
        self.assertEqual(status["failed_polls"], 23)
        self.assertEqual(status["successful_polls"], 0)
        self.assertFalse(status["seeded"])
        self.assertIsNone(status["last_success_at"])
        self.assertEqual(status["backoff_seconds"], 120)
        self.assertEqual(status["last_attempt_ms"], 65)
        self.assertEqual(monitor.stop_event.waits[-1], 120)
        # 23 attempts occur over >30 minutes because failures cause real backoff.
        self.assertGreater(sum(monitor.stop_event.waits[:-1]), 30 * 60)

    def test_recovery_clears_active_error_and_resumes_target_cadence(self):
        class Client:
            calls = 0

            def search(self):
                self.calls += 1
                if self.calls == 1:
                    raise ApiFailure("Temporaire", kind="timeout")
                return [], 2

            def close(self):
                pass

        class SimulatedWait:
            waits = []

            def is_set(self):
                return len(self.waits) >= 2

            def wait(self, delay=None):
                self.waits.append(delay)
                return self.is_set()

            def set(self):
                pass

        monitor = Monitor(Settings(), ":memory:", client=Client())
        self.addCleanup(monitor.close)
        monitor.stop_event = SimulatedWait()
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        status = monitor.state()["status"]
        self.assertEqual((status["polls"], status["successful_polls"], status["failed_polls"]), (2, 1, 1))
        self.assertEqual(status["phase"], "watching")
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertEqual(status["backoff_seconds"], 0)
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["last_error_details"])
        self.assertLessEqual(monitor.stop_event.waits[-1], 1)
