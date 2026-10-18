import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from riven_sniper.core import (
    ApiFailure, MarketClient, Monitor, Settings, auction_id_from_input, normalize, retry_delay,
)


def auction(listing_id="listing-1", price=100, **changes):
    result = {
        "id": listing_id, "buyout_price": price, "starting_price": 1,
        "created": "2026-01-01T00:00:00.000Z", "visible": True, "closed": False,
        "item": {"type": "riven", "weapon_url_name": "dual_toxocyst", "name": "Test riven"},
        "owner": {"platform": "pc", "ingame_name": "Seller", "status": "ingame"},
    }
    result.update(changes)
    return result


class StubClient:
    def close(self):
        pass


class FilteringTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_buyout_is_used_even_when_starting_bid_is_in_range(self):
        self.assertFalse(normalize(auction(price=2001, starting_price=10), self.settings)["eligible"])
        self.assertTrue(normalize(auction(price=10, starting_price=9999), self.settings)["eligible"])
        self.assertFalse(normalize(auction(price=None, starting_price=100), self.settings)["eligible"])

    def test_price_bounds_are_inclusive_and_must_be_integer_prices(self):
        for price, expected in [(9, False), (10, True), (2000, True), (2001, False),
                                (-1, False), (True, False), (100.0, False), ("100", False)]:
            with self.subTest(price=price):
                self.assertEqual(normalize(auction(price=price), self.settings)["eligible"], expected)

    def test_stats_rolls_and_seller_presence_do_not_filter_a_valid_listing(self):
        for rolls, attributes in [(0, []), (99999, [{"url_name": "damage", "value": -99}])]:
            raw = auction()
            raw["item"].update(re_rolls=rolls, attributes=attributes, mastery_level=16, polarity="madurai")
            raw["owner"]["status"] = "offline"
            self.assertTrue(normalize(raw, self.settings)["eligible"])

    def test_other_weapons_and_platforms_are_excluded(self):
        for weapon, platform in [("torid", "pc"), ("dual_toxocyst", "ps4"), ("dual_toxocyst", "xbox")]:
            raw = auction()
            raw["item"]["weapon_url_name"] = weapon
            raw["owner"]["platform"] = platform
            self.assertIsNone(normalize(raw, self.settings))
        raw = auction()
        del raw["owner"]["platform"]
        self.assertTrue(normalize(raw, self.settings)["eligible"])

    def test_hidden_closed_and_private_listings_are_ineligible(self):
        for change in [{"closed": True}, {"visible": False}, {"private": True}]:
            with self.subTest(change=change):
                self.assertFalse(normalize(auction(**change), self.settings)["eligible"])

    def test_malformed_listings_do_not_generate_a_usable_link(self):
        for change in [{"id": "../bad"}, {"id": ""}, {"id": 1}, {"owner": None}, {"item": []}]:
            with self.subTest(change=change):
                self.assertIsNone(normalize(auction(**change), self.settings))
        self.assertIsNone(normalize(auction(created="not-a-date"), self.settings)["created_at"])
        self.assertIsNone(normalize(auction(), self.settings, demo=True)["url"])

    def test_subsecond_polling_and_invalid_ranges_are_rejected(self):
        for changes in [{"interval_seconds": 0.99}, {"interval_seconds": float("nan")},
                        {"interval_seconds": float("inf")}, {"min_price": -1},
                        {"min_price": 11, "max_price": 10}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(**changes)


class AuctionInputTests(unittest.TestCase):
    def test_accepts_an_id_or_an_official_https_auction_link(self):
        for value in ["abc-123_X", "  abc-123_X  ",
                      "https://warframe.market/auction/abc-123_X",
                      "https://warframe.market/fr/auction/abc-123_X/",
                      "https://www.warframe.market/en/auction/abc-123_X"]:
            with self.subTest(value=value):
                self.assertEqual(auction_id_from_input(value), "abc-123_X")

    def test_rejects_hostile_hosts_credentials_and_malformed_links(self):
        for value in ["https://warframe.market.evil.invalid/auction/id",
                      "https://evil.invalid/auction/id",
                      "https://warframe.market@evil.invalid/auction/id",
                      "https://user:password@warframe.market/auction/id",
                      "http://warframe.market/auction/id",
                      "https://warframe.market:bad/auction/id",
                      "https://warframe.market/auctions",
                      "https://warframe.market/fr/auction/id/other",
                      "https://warframe.market/auction/../id",
                      "//warframe.market/auction/id", "../id", "", "id with spaces"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                auction_id_from_input(value)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "observations.sqlite3"
        self.monitor = Monitor(Settings(), self.database, client=StubClient())
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()

    def tearDown(self):
        self.stdout.__exit__(None, None, None)
        self.monitor.close()
        self.temp.cleanup()

    def test_first_snapshot_is_silent_and_repeated_snapshot_does_not_alert(self):
        listings = [auction("first", 10), auction("second", 2000), auction("outside", 2001)]
        self.assertEqual(self.monitor.ingest(listings, 42.5), 0)
        self.assertEqual(self.output.getvalue(), "")
        state = self.monitor.state()
        self.assertEqual(state["status"]["baseline_count"], 2)
        self.assertEqual({offer["kind"] for offer in state["offers"]}, {"baseline"})
        self.assertEqual(self.monitor.ingest(listings, 3), 0)
        self.assertEqual(len(self.monitor.state()["offers"]), 2)
        self.assertEqual(self.output.getvalue(), "")

    def test_new_price_and_threshold_changes_alert_once_each(self):
        self.monitor.ingest([auction("old", 2500)], 1)
        self.assertEqual(self.monitor.ingest([auction("new", 2000), auction("old", 2000)], 2), 2)
        self.assertEqual(self.monitor.ingest([auction("new", 2000), auction("old", 2000)], 2), 0)
        self.assertEqual(self.monitor.ingest([auction("new", 1999)], 2), 1)
        offers = {offer["id"]: offer for offer in self.monitor.state()["offers"]}
        self.assertEqual(offers["new"]["kind"], "price_change")
        self.assertEqual(offers["old"]["kind"], "price_change")
        self.assertEqual(offers["new"]["price"], 1999)
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["price_change", "new", "price_change"])

    def test_duplicate_rows_in_one_snapshot_do_not_double_alert(self):
        self.monitor.ingest([], 1)
        self.assertEqual(self.monitor.ingest([auction(), auction()], 1), 1)
        self.assertEqual(len(self.monitor.state()["offers"]), 1)

    def test_visibility_return_and_threshold_reentry_are_detected(self):
        self.monitor.ingest([auction()], 1)
        self.assertEqual(self.monitor.ingest([auction(visible=False)], 1), 0)
        self.assertEqual(self.monitor.ingest([auction()], 1), 1)
        self.assertEqual(self.monitor.ingest([auction(price=2001)], 1), 0)
        self.assertEqual(self.monitor.ingest([auction(price=2000)], 1), 1)

    def test_persistent_seen_and_history_survive_restart_without_realerting(self):
        self.monitor.ingest([], 1)
        self.monitor.ingest([auction()], 1)
        event_id = self.monitor.state()["offers"][0]["event_id"]
        self.monitor.close()
        self.monitor = Monitor(Settings(), self.database, client=StubClient())
        self.assertEqual(self.monitor.ingest([auction()], 1), 0)
        self.assertEqual(self.monitor.state()["offers"][0]["event_id"], event_id)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.monitor.export_csv())))), 1)

    def test_comparison_records_signed_lead_and_rejects_duplicate(self):
        self.monitor.ingest([], 1)
        detected = datetime.now(timezone.utc) - timedelta(seconds=4)
        self.monitor.ingest([auction()], 1, observed_at=detected.isoformat())
        event_id = self.monitor.state()["offers"][0]["event_id"]
        received = detected + timedelta(seconds=2.25)
        self.monitor.compare("listing-1", received.isoformat(), event_id)
        comparison = self.monitor.state()["comparison"]
        self.assertEqual(comparison["count"], 1)
        self.assertEqual(comparison["median_lead_seconds"], 2.25)
        with self.assertRaises(ValueError):
            self.monitor.compare("listing-1", received.isoformat(), event_id)

    def test_negative_lead_means_aleca_arrived_first(self):
        self.monitor.ingest([], 1)
        detected = datetime.now(timezone.utc)
        self.monitor.ingest([auction()], 1, observed_at=detected.isoformat())
        self.monitor.compare("listing-1", (detected - timedelta(seconds=3)).isoformat())
        self.assertEqual(self.monitor.state()["comparison"]["median_lead_seconds"], -3)

    def test_comparison_rejects_baseline_missing_old_event_and_bad_clock(self):
        self.monitor.ingest([auction("baseline")], 1)
        for listing_id in ["baseline", "missing"]:
            with self.subTest(listing_id=listing_id), self.assertRaises(ValueError):
                self.monitor.compare(listing_id)
        self.monitor.ingest([auction()], 1)
        old_event_id = self.monitor.state()["offers"][0]["event_id"]
        self.monitor.ingest([auction(price=99)], 1)
        with self.assertRaises(ValueError):
            self.monitor.compare("listing-1", event_id=old_event_id)
        for received in ["invalid", "2026-01-01T00:00:00", (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()]:
            with self.subTest(received=received), self.assertRaises(ValueError):
                self.monitor.compare("listing-1", received)
        self.assertEqual(self.monitor.state()["comparison"]["count"], 0)

    def test_csv_preserves_all_events_and_neutralizes_spreadsheet_formulas(self):
        self.monitor.ingest([], 1)
        raw = auction()
        raw["item"]["name"] = '=HYPERLINK("https://example.invalid")'
        raw["owner"]["ingame_name"] = "+SUM(1,1)"
        self.monitor.ingest([raw], 1)
        raw["buyout_price"] = 101
        self.monitor.ingest([raw], 1)
        rows = list(csv.DictReader(io.StringIO(self.monitor.export_csv())))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["price"] for row in rows], ["100", "101"])
        self.assertTrue(all(row["name"].startswith("'=") and row["seller"].startswith("'+") for row in rows))
        self.assertEqual({row["mode"] for row in rows}, {"live"})

    def test_aleca_before_sniper_is_queued_then_records_a_negative_lead(self):
        self.monitor.ingest([], 1)
        received = datetime.now(timezone.utc).replace(microsecond=0)
        self.monitor.record_aleca("https://warframe.market/fr/auction/early-id", 100, received.isoformat())
        state = self.monitor.state()
        self.assertEqual(state["comparison"]["pending_count"], 1)
        self.assertEqual(state["comparison"]["count"], 0)
        self.assertEqual(state["offers"], [])
        detected = received + timedelta(seconds=3)
        self.monitor.ingest([auction("early-id", 100)], 1, observed_at=detected.isoformat())
        comparison = self.monitor.state()["comparison"]
        self.assertEqual(comparison["pending_count"], 0)
        self.assertEqual(comparison["count"], 1)
        self.assertEqual(comparison["median_lead_seconds"], -3)

    def test_wrong_price_does_not_consume_a_pending_aleca_receipt(self):
        self.monitor.ingest([], 1)
        received = datetime.now(timezone.utc).replace(microsecond=0)
        self.monitor.record_aleca("early-id", 100, received.isoformat())
        self.monitor.ingest([auction("early-id", 101)], 1,
                            observed_at=(received + timedelta(seconds=1)).isoformat())
        self.assertEqual(self.monitor.state()["comparison"]["pending_count"], 1)
        self.assertEqual(self.monitor.state()["comparison"]["count"], 0)
        self.monitor.ingest([auction("early-id", 100)], 1,
                            observed_at=(received + timedelta(seconds=2)).isoformat())
        state = self.monitor.state()
        self.assertEqual(state["comparison"]["pending_count"], 0)
        self.assertEqual(state["comparison"]["median_lead_seconds"], -2)
        self.assertEqual(state["offers"][0]["kind"], "price_change")

    def test_duplicate_aleca_receipt_cannot_overwrite_earliest_timestamp(self):
        self.monitor.ingest([], 1)
        received = datetime.now(timezone.utc).replace(microsecond=0)
        self.monitor.record_aleca("early-id", 100, received.isoformat())
        with self.assertRaises(ValueError):
            self.monitor.record_aleca("early-id", 100, (received + timedelta(seconds=1)).isoformat())
        self.monitor.ingest([auction("early-id", 100)], 1,
                            observed_at=(received + timedelta(seconds=3)).isoformat())
        self.assertEqual(self.monitor.state()["comparison"]["median_lead_seconds"], -3)
        with self.assertRaises(ValueError):
            self.monitor.record_aleca("early-id", 100, received.isoformat())

    def test_aleca_receipt_rejects_unseeded_and_blocked_monitors(self):
        with self.assertRaises(ValueError):
            self.monitor.record_aleca("early-id", 100)
        self.monitor.ingest([], 1)
        self.monitor.status["phase"] = "blocked"
        with self.assertRaises(ValueError):
            self.monitor.record_aleca("early-id", 100)
        self.assertEqual(self.monitor.state()["comparison"]["pending_count"], 0)

    def test_aleca_receipt_cannot_compare_a_known_baseline(self):
        self.monitor.ingest([auction()], 1)
        with self.assertRaises(ValueError):
            self.monitor.record_aleca("listing-1", 100)
        self.assertEqual(self.monitor.state()["comparison"]["count"], 0)
        self.assertEqual(self.monitor.state()["comparison"]["pending_count"], 0)

    def test_aleca_receipt_can_match_an_existing_new_event_immediately(self):
        self.monitor.ingest([], 1)
        detected = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=2)
        self.monitor.ingest([auction()], 1, observed_at=detected.isoformat())
        self.monitor.record_aleca("listing-1", 100, (detected + timedelta(seconds=1)).isoformat())
        comparison = self.monitor.state()["comparison"]
        self.assertEqual(comparison["count"], 1)
        self.assertEqual(comparison["pending_count"], 0)
        self.assertEqual(comparison["median_lead_seconds"], 1)

    def test_aleca_receipt_validates_price_and_clock_before_queueing(self):
        self.monitor.ingest([], 1)
        for price in [None, "100", True, 9, 2001]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.monitor.record_aleca("early-id", price)
        for received in ["invalid", "2026-01-01T00:00:00",
                         (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()]:
            with self.subTest(received=received), self.assertRaises(ValueError):
                self.monitor.record_aleca("early-id", 100, received)
        self.assertEqual(self.monitor.state()["comparison"]["pending_count"], 0)


class MarketClientTests(unittest.TestCase):
    def make_client(self, handler):
        client = MarketClient(Settings(), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_uses_one_global_pc_feed_without_unsupported_query_parameters(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"payload": {"auctions": [auction()]}})

        listings, elapsed = self.make_client(handler).search()
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.warframe.market/v1/auctions")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Platform"], "pc")
        self.assertEqual(listings[0]["id"], "listing-1")
        self.assertGreaterEqual(elapsed, 0)

    def test_429_preserves_retry_after_without_retrying_in_the_client(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "17"})

        with self.assertRaises(ApiFailure) as caught:
            self.make_client(handler).search()
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertFalse(caught.exception.blocked)
        self.assertEqual(len(requests), 1)

    def test_retry_after_accepts_http_dates_and_rejects_invalid_values(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_delay("Thu, 01 Jan 2026 00:00:12 GMT", now), 12)
        self.assertEqual(retry_delay("-5", now), 0)
        for value in [None, "not-a-date", "nan", "inf"]:
            with self.subTest(value=value):
                self.assertIsNone(retry_delay(value, now))

    def test_forbidden_access_is_terminal_and_does_not_echo_response_secrets(self):
        with self.assertRaises(ApiFailure) as caught:
            self.make_client(lambda request: httpx.Response(403, text="secret-token")).search()
        self.assertTrue(caught.exception.blocked)
        self.assertNotIn("secret-token", str(caught.exception))

    def test_invalid_response_schema_is_terminal(self):
        for payload in [{}, {"payload": []}, {"payload": {"auctions": {}}},
                        {"payload": {"auctions": [None]}}]:
            with self.subTest(payload=payload), self.assertRaises(ApiFailure) as caught:
                self.make_client(lambda request, p=payload: httpx.Response(200, json=p)).search()
            self.assertTrue(caught.exception.blocked)
        with self.assertRaises(ApiFailure):
            self.make_client(lambda request: httpx.Response(200, text="not JSON")).search()

    def test_tls_failure_is_reported_without_leaking_proxy_credentials(self):
        def handler(request):
            raise httpx.ConnectError("TLS verification failed via https://user:secret@proxy.invalid", request=request)

        with self.assertRaises(ApiFailure) as caught:
            self.make_client(handler).search()
        self.assertFalse(caught.exception.blocked)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("TLS", str(caught.exception))


class StopAfterWait:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, delay=None):
        self.waits.append(delay)
        self.stopped = True
        return True

    def set(self):
        self.stopped = True


class SchedulerTests(unittest.TestCase):
    def make_monitor(self, client):
        monitor = Monitor(Settings(), ":memory:", client=client)
        monitor.stop_event = StopAfterWait()
        self.addCleanup(monitor.close)
        return monitor

    def test_fast_success_waits_out_the_one_second_cadence(self):
        class Client(StubClient):
            def search(self):
                return [], 10

        monitor = self.make_monitor(Client())
        with patch("riven_sniper.core.time.monotonic", side_effect=[100, 100.2]):
            monitor.run()
        self.assertAlmostEqual(monitor.stop_event.waits[0], 0.8)
        self.assertEqual(monitor.state()["status"]["phase"], "watching")

    def test_quota_backoff_honors_server_retry_after(self):
        class Client(StubClient):
            def search(self):
                raise ApiFailure("quota", retry_after=17)

        monitor = self.make_monitor(Client())
        monitor.run()
        self.assertEqual(monitor.stop_event.waits, [17])
        self.assertEqual(monitor.state()["status"]["phase"], "backoff")

    def test_access_block_stops_polling_until_explicit_shutdown(self):
        class Client(StubClient):
            calls = 0

            def search(self):
                self.calls += 1
                raise ApiFailure("forbidden", blocked=True)

        client = Client()
        monitor = self.make_monitor(client)
        monitor.run()
        self.assertEqual(client.calls, 1)
        self.assertEqual(monitor.stop_event.waits, [None])
        self.assertEqual(monitor.state()["status"]["phase"], "blocked")


if __name__ == "__main__":
    unittest.main()
