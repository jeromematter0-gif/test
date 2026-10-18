import contextlib
import http.client
import io
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone

from riven_sniper.core import Monitor, Settings
from riven_sniper.server import create_server


class StubClient:
    def close(self):
        pass


def listing():
    return {
        "id": "server-listing", "buyout_price": 100,
        "item": {"type": "riven", "weapon_url_name": "dual_toxocyst", "name": "Riven"},
        "owner": {"platform": "pc", "ingame_name": "Seller"},
        "closed": False, "visible": True,
    }


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(Settings(), ":memory:", client=StubClient())
        self.server = create_server(self.monitor, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.monitor.close()

    def request(self, method, path, data=None, headers=None, raw=None):
        request_headers = dict(headers or {})
        body = raw
        if data is not None:
            body = json.dumps(data)
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()
        return result

    def test_dashboard_assets_and_state_are_served_with_local_security_headers(self):
        for path, content_type in [("/", "text/html"), ("/app.js", "text/javascript"),
                                   ("/styles.css", "text/css"), ("/api/state", "application/json")]:
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertGreater(len(body), 0)
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual(state["config"]["weapon"], "dual_toxocyst")

    def test_csv_export_has_utf8_bom_and_csv_content(self):
        status, headers, body = self.request("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))
        self.assertTrue(body.startswith(b"\xef\xbb\xbfmode,event_id,"))

    def test_unknown_and_traversal_paths_are_not_served(self):
        for path in ["/missing", "/../core.py", "/static/../core.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_foreign_host_is_rejected_even_on_read_endpoints(self):
        for method, path in [("GET", "/api/state"), ("GET", "/"), ("POST", "/api/compare")]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, headers={"Host": "attacker.invalid"})[0], 403)

    def test_foreign_origin_cannot_mutate_a_comparison(self):
        for endpoint in ["/api/compare", "/api/aleca"]:
            with self.subTest(endpoint=endpoint):
                status, _, body = self.request("POST", endpoint, data={"id": "server-listing", "price": 100},
                                               headers={"Origin": "https://attacker.invalid"})
                self.assertEqual(status, 403)
                self.assertIn("error", json.loads(body))

    def test_post_validation_rejects_bad_bodies_and_content_types(self):
        cases = [
            {"data": []}, {"data": {"id": 42}}, {"data": {"id": "x", "received_at": 42}},
            {"raw": "{bad", "headers": {"Content-Type": "application/json"}},
            {"raw": "{}", "headers": {"Content-Type": "text/plain"}},
            {"raw": "x" * 4097, "headers": {"Content-Type": "application/json"}},
            {"raw": "", "headers": {"Content-Type": "application/json"}},
        ]
        for case in cases:
            with self.subTest(case=case):
                status, _, body = self.request("POST", "/api/compare", **case)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.request("POST", "/api/missing", data={"id": "x"})[0], 404)

    def test_compare_endpoint_records_exact_event_and_rejects_duplicates(self):
        self.monitor.ingest([], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.monitor.ingest([listing()], 1)
        event_id = self.monitor.state()["offers"][0]["event_id"]
        payload = {"id": "server-listing", "event_id": event_id}
        origin = f"http://127.0.0.1:{self.server.server_port}"
        status, _, body = self.request("POST", "/api/compare", data=payload, headers={"Origin": origin})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["comparison"]["count"], 1)
        self.assertEqual(self.request("POST", "/api/compare", data=payload)[0], 400)

    def test_aleca_endpoint_preserves_a_receipt_before_sniper_sees_the_offer(self):
        self.monitor.ingest([], 1)
        received = datetime.now(timezone.utc).replace(microsecond=0)
        payload = {"id": "https://warframe.market/fr/auction/server-listing", "price": 100,
                   "received_at": received.isoformat()}
        status, _, body = self.request("POST", "/api/aleca", data=payload)
        self.assertEqual(status, 200)
        comparison = json.loads(body)["comparison"]
        self.assertEqual(comparison["pending_count"], 1)
        self.assertEqual(comparison["count"], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.monitor.ingest([listing()], 1,
                                observed_at=(received + timedelta(seconds=2)).isoformat())
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual(state["comparison"]["pending_count"], 0)
        self.assertEqual(state["comparison"]["median_lead_seconds"], -2)
        self.assertEqual(self.request("POST", "/api/aleca", data=payload)[0], 400)

    def test_aleca_endpoint_rejects_invalid_price_urls_and_unseeded_monitor(self):
        self.assertEqual(self.request("POST", "/api/aleca", data={"id": "early-id", "price": 100})[0], 400)
        self.monitor.ingest([], 1)
        for payload in [{"id": "early-id"}, {"id": "early-id", "price": "100"},
                        {"id": "early-id", "price": True}, {"id": "early-id", "price": 2001},
                        {"id": "https://evil.invalid/auction/id", "price": 100}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/aleca", data=payload)[0], 400)
        self.assertEqual(self.monitor.state()["comparison"]["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
