import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx

from riven_sniper.core import ApiFailure, MarketClient, Monitor, Settings


def listing(listing_id="burston-old", price=400, weapon="burston", updated="2026-10-18T16:52:00Z"):
    return {"id": listing_id, "created": "2026-10-14T10:00:00Z", "updated": updated,
            "buyout_price": price, "starting_price": 1, "visible": True, "closed": False,
            "item": {"type": "riven", "weapon_url_name": weapon, "name": "Saticron"},
            "owner": {"platform": "pc", "ingame_name": "Player", "status": "ingame"}}


class Stub:
    def close(self):
        pass


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(Settings(), ":memory:", client=Stub())
        self.addCleanup(self.monitor.close)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_first_snapshot_of_each_source_is_silent_but_visible(self):
        self.monitor.ingest([], 1, source="recent")
        self.assertEqual(self.monitor.ingest([listing()], 2, source="weapon:burston"), 0)
        offer = self.monitor.state()["offers"][0]
        self.assertEqual(offer["kind"], "baseline")
        self.assertEqual(offer["source_label"], "Recherche Burston")
        self.assertEqual(self.monitor.state()["discord"]["pending_count"], 0)
        self.assertEqual(self.output.getvalue(), "")

    def test_old_price_change_absent_from_recent_feed_is_detected_by_weapon_search(self):
        self.monitor.ingest([], 1, source="recent")
        self.monitor.ingest([listing(price=800, updated="2026-10-14T10:00:00Z")], 1, source="weapon:burston")
        self.monitor.ingest([], 1, source="recent")
        self.assertEqual(self.monitor.ingest([listing(price=400)], 1, source="weapon:burston"), 1)
        offer = self.monitor.state()["offers"][0]
        self.assertEqual(offer["kind"], "price_change")
        self.assertEqual(offer["price"], 400)
        self.assertEqual(offer["created_at"], "2026-10-14T10:00:00Z")
        self.assertEqual(offer["source"], "weapon:burston")

    def test_torid_available_in_weapon_search_before_global_keeps_earlier_detection(self):
        self.monitor.ingest([], 1, source="recent")
        self.monitor.ingest([], 1, source="weapon:torid")
        offer = listing("torid-new", 370, "torid", "2026-10-18T16:35:32Z")
        offer["created"] = "2026-10-18T16:35:32Z"
        self.assertEqual(self.monitor.ingest([offer], 1, source="weapon:torid", observed_at="2026-10-18T16:35:39Z"), 1)
        self.assertEqual(self.monitor.ingest([offer], 1, source="recent", observed_at="2026-10-18T16:36:01.298Z"), 0)
        row = self.monitor.state()["offers"][0]
        self.assertEqual(row["detected_at"], "2026-10-18T16:35:39Z")
        self.assertEqual(row["source"], "weapon:torid")
        self.assertEqual(len(self.monitor.db.execute("SELECT * FROM events WHERE kind='new'").fetchall()), 1)

    def test_older_cached_price_cannot_reverse_newer_price_or_send_a_false_update(self):
        self.monitor.ingest([listing(price=600, updated="2026-10-18T16:00:00Z")], 1, source="recent")
        self.assertEqual(self.monitor.ingest([listing(price=400)], 1, source="weapon:burston"), 1)
        self.assertEqual(self.monitor.ingest([listing(price=600, updated="2026-10-18T16:00:00Z")], 1, source="recent"), 0)
        seen = self.monitor.db.execute("SELECT price FROM seen WHERE id='burston-old'").fetchone()[0]
        self.assertEqual(seen, 400)
        self.assertEqual(self.monitor.state()["status"]["ignored_stale"], 1)
        self.assertEqual(self.monitor.state()["offers"][0]["price"], 400)

    def test_missing_edit_dates_remain_usable_without_claiming_revision_protection(self):
        raw = listing(updated=None, price=800)
        self.monitor.ingest([raw], 1, source="weapon:burston")
        raw["buyout_price"] = 400
        self.assertEqual(self.monitor.ingest([raw], 1, source="weapon:burston"), 1)
        self.assertIsNone(self.monitor.state()["offers"][0]["updated_at"])

    def test_repeated_snapshots_and_possible_result_limit_are_exposed(self):
        raw = [listing(str(i)) for i in range(100)]
        self.monitor.ingest(raw, 1, source="weapon:burston", observed_at="2026-10-18T16:00:00Z")
        self.monitor.ingest(raw, 1, source="weapon:burston", observed_at="2026-10-18T16:01:00Z")
        stats = next(s for s in self.monitor.state()["sources"] if s["id"] == "weapon:burston")
        self.assertTrue(stats["possibly_limited"])
        self.assertEqual(stats["unchanged_responses"], 1)
        self.assertEqual(stats["snapshot_changed_at"], "2026-10-18T16:00:00Z")
        self.assertEqual(stats["last_success_at"], "2026-10-18T16:01:00Z")

    def test_csv_and_discord_payload_preserve_source(self):
        self.monitor.discord.configure({"webhook_url": "https://discord.com/api/webhooks/1/this_is_a_fake_test_token_12345", "enabled": True})
        self.monitor.ingest([], 1, source="recent")
        self.monitor.ingest([], 1, source="weapon:burston")
        self.monitor.ingest([listing()], 1, source="weapon:burston")
        self.monitor.ingest([listing()], 1, source="recent")
        payloads = self.monitor.db.execute("SELECT payload FROM discord_outbox").fetchall()
        self.assertEqual(len(payloads), 1)
        self.assertIn("Recherche Burston", payloads[0][0])
        self.assertIn("weapon:burston", self.monitor.export_csv())


class RoutingTests(unittest.TestCase):
    def test_weapon_request_uses_documented_query_and_keeps_one_http_call(self):
        requests = []
        client = MarketClient(Settings(), transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"payload": {"auctions": []}})))
        self.addCleanup(client.close)
        client.search(source="weapon:burston")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/v1/auctions/search")
        self.assertEqual(dict(requests[0].url.params), {"type": "riven", "weapon_url_name": "burston", "buyout_policy": "with", "sort_by": "price_asc"})
        self.assertEqual(requests[0].headers["Platform"], "pc")
        self.assertEqual(client.last_response_meta["source"], "weapon:burston")

    def test_modes_have_explicit_source_cycles_and_reject_unknown_modes(self):
        self.assertEqual(len(Settings().sources()), 6)
        self.assertEqual(Settings(collection_mode="recent").sources(), ("recent",))
        self.assertEqual(len(Settings(collection_mode="weapons").sources()), 5)
        with self.assertRaises(ValueError):
            Settings(collection_mode="unknown")

    def test_global_budget_and_fair_rotation_are_shared_by_all_six_sources(self):
        now = [0.0]
        calls = []
        class Client(Stub):
            def search(self, source="recent"):
                calls.append((source, now[0]))
                now[0] += 0.2
                return [], 200
        class Wait:
            def is_set(self): return len(calls) >= 12
            def wait(self, delay=None): now[0] += delay or 0
            def set(self): pass
        monitor = Monitor(Settings(collection_mode="hybrid"), ":memory:", client=Client())
        self.addCleanup(monitor.close)
        monitor.stop_event = Wait()
        with patch("riven_sniper.core.time.monotonic", side_effect=lambda: now[0]):
            monitor.run()
        self.assertEqual([s for s, t in calls], list(Settings().sources()) * 2)
        self.assertTrue(all(b[1] - a[1] >= 0.999 for a, b in zip(calls, calls[1:])))
        self.assertEqual(monitor.state()["status"]["successful_polls"], 12)
        self.assertEqual(monitor.state()["status"]["warmup_remaining"], 0)
        self.assertTrue(all(s["attempts"] == 2 and s["successes"] == 2 for s in monitor.state()["sources"]))

    def test_quota_pause_applies_to_next_weapon_not_only_the_failing_source(self):
        now, calls = [0.0], []
        class Client(Stub):
            def search(self, source="recent"):
                calls.append((source, now[0]))
                if len(calls) == 1: raise ApiFailure("Quota", retry_after=7, kind="quota")
                return [], 1
        class Wait:
            def is_set(self): return len(calls) >= 2
            def wait(self, delay=None): now[0] += delay or 0
            def set(self): pass
        monitor = Monitor(Settings(), ":memory:", client=Client())
        self.addCleanup(monitor.close); monitor.stop_event = Wait()
        with patch("riven_sniper.core.time.monotonic", side_effect=lambda: now[0]), contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        self.assertEqual(calls, [("recent", 0), ("weapon:burston", 7)])
