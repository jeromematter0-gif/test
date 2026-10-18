import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from riven_sniper.core import DEFAULT_WATCHES, MarketClient, Monitor, Settings, normalize
from riven_sniper.notifications import validate_webhook

# Deliberately invalid real Discord ID/token; only ever used with MockTransport.
HOOK = "https://discord.com/api/webhooks/1/this_is_a_fake_test_token_12345"
OTHER_HOOK = "https://discord.com/api/webhooks/2/another_fake_test_token_12345"


def listing(weapon="dual_toxocyst", price=100, listing_id=None):
    return {"id": listing_id or weapon + "-1", "buyout_price": price, "starting_price": 1,
            "visible": True, "closed": False, "created": "2026-10-18T10:00:00Z",
            "item": {"type": "riven", "weapon_url_name": weapon, "name": "crita-visitis", "re_rolls": 70,
                     "mastery_level": 10, "attributes": [{"url_name": "critical_chance", "value": 123.4, "positive": True}]},
            "owner": {"platform": "pc", "ingame_name": "TznMinos", "status": "ingame"}}


class Stub:
    def close(self):
        pass


class MultiWeaponTests(unittest.TestCase):
    def test_five_exact_user_ranges_and_inclusive_boundaries(self):
        settings = Settings()
        self.assertEqual([(w.weapon, w.min_price, w.max_price) for w in settings.watches], [
            ("burston", 10, 401), ("dual_toxocyst", 10, 201), ("torid", 10, 370),
            ("verglas", 10, 350), ("magistar", 10, 501)])
        for watch in settings.watches:
            for price, eligible in [(9, False), (10, True), (watch.max_price, True), (watch.max_price + 1, False)]:
                with self.subTest(weapon=watch.weapon, price=price):
                    self.assertEqual(normalize(listing(watch.weapon, price), settings)["eligible"], eligible)

    def test_single_feed_request_yields_all_five_weapons_without_enrichment_requests(self):
        requests = []
        auctions = [listing(w.weapon, w.max_price) for w in DEFAULT_WATCHES]

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"payload": {"auctions": auctions}})

        client = MarketClient(Settings(), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        raw, ms = client.search()
        monitor = Monitor(Settings(), ":memory:", client=Stub())
        self.addCleanup(monitor.close)
        monitor.ingest(raw, ms)
        self.assertEqual(len(requests), 1)
        self.assertEqual({o["weapon"] for o in monitor.state()["offers"]}, {w.weapon for w in DEFAULT_WATCHES})

    def test_full_riven_details_and_copy_message_use_actual_seller_name_price(self):
        offer = normalize(listing(price=201), Settings())
        self.assertEqual(offer["full_name"], "Dual Toxocyst Crita-visitis")
        self.assertEqual(offer["rerolls"], 70)
        self.assertEqual(offer["mastery_level"], 10)
        self.assertEqual(offer["attributes"][0]["value"], 123.4)
        self.assertEqual(offer["trading_message"], "/w TznMinos Hi, I am interested in buying your Dual Toxocyst Crita-visitis riven for 201 platinum, listed on Warframe.Market.")
        raw = listing(); raw["owner"]["ingame_name"] = "bad\n/w Other"
        self.assertIsNone(normalize(raw, Settings())["trading_message"])

    def test_old_database_is_migrated_and_old_dual_prices_above201_are_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            old = sqlite3.connect(path)
            old.execute("""CREATE TABLE events(event_id INTEGER PRIMARY KEY, id TEXT, weapon TEXT, name TEXT,
                price INTEGER, seller TEXT, seller_status TEXT, created_at TEXT, detected_at TEXT, api_age_seconds REAL,
                request_ms REAL, kind TEXT, url TEXT, aleca_seen_at TEXT, lead_seconds REAL)""")
            for listing_id, price in [("expensive", 700), ("cheap", 100)]:
                old.execute("INSERT INTO events(id,weapon,name,price,seller,seller_status,detected_at,request_ms,kind) VALUES (?,?,?,?,?,?,?,?,?)",
                            (listing_id, "dual_toxocyst", "Legacy", price, "Player", "ingame", "2026-10-18T10:00:00Z", 1, "baseline"))
            old.commit()
            old.close()
            current = Monitor(Settings(), path, client=Stub())
            try:
                self.assertEqual([o["id"] for o in current.state()["offers"]], ["cheap"])
                self.assertIn("100", current.export_csv())
                self.assertNotIn(",700,", current.export_csv())
            finally:
                current.close()


class DiscordTests(unittest.TestCase):
    def make_monitor(self, handler=None, config_path=None, demo=False):
        monitor = Monitor(Settings(), ":memory:", client=Stub(), demo=demo,
                          discord_config_path=config_path,
                          discord_transport=httpx.MockTransport(handler or (lambda request: httpx.Response(204))))
        self.addCleanup(monitor.close)
        return monitor

    def enable(self, monitor):
        monitor.discord.configure({"webhook_url": HOOK, "enabled": True})

    def test_validation_rejects_insecure_or_arbitrary_destinations_without_echoing_secret(self):
        self.assertEqual(validate_webhook(HOOK), HOOK)
        for value in [HOOK.replace("https:", "http:"), HOOK.replace("discord.com", "discord.com.evil.invalid"),
                      HOOK.replace("discord.com", "127.0.0.1"), HOOK + "?token=secret", HOOK + "#secret",
                      HOOK.replace("discord.com", "user:secret@discord.com"), "secret"]:
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                validate_webhook(value)
            self.assertNotIn("secret", str(caught.exception))

    def test_no_webhook_means_no_queue_and_no_network_request(self):
        requests = []
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(204))
        monitor.ingest([], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.ingest([listing()], 1)
        monitor.discord.run_once()
        self.assertEqual(requests, [])
        self.assertEqual(monitor.state()["discord"]["pending_count"], 0)

    def test_baseline_silent_new_offer_sent_once_with_details_and_no_mentions(self):
        requests = []
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(200, json={"id": "test"}))
        self.enable(monitor)
        monitor.ingest([listing(listing_id="baseline")], 1)
        self.assertEqual(monitor.state()["discord"]["pending_count"], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.ingest([listing()], 1)
            monitor.ingest([listing()], 1)
        self.assertEqual(monitor.state()["discord"]["pending_count"], 1)
        monitor.discord.run_once()
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        embed = payload["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertIn("Crita-visitis", embed["title"])
        self.assertEqual(fields["Prix immédiat"], "100 pl")
        self.assertEqual(fields["Vendeur"], "`TznMinos`")
        self.assertEqual(fields["Rolls"], "70")
        self.assertIn("/w TznMinos", fields["Message à copier"])
        self.assertIn("123.4", fields["Attributs"])
        self.assertEqual(monitor.state()["discord"]["sent_count"], 1)

    def test_slow_discord_post_does_not_block_the_collector(self):
        entered, release = threading.Event(), threading.Event()

        def handler(request):
            entered.set()
            release.wait(3)
            return httpx.Response(204)

        monitor = self.make_monitor(handler)
        self.enable(monitor); monitor.ingest([], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.ingest([listing()], 1)
        sending = threading.Thread(target=monitor.discord.run_once)
        sending.start()
        try:
            self.assertTrue(entered.wait(2))
            with contextlib.redirect_stdout(io.StringIO()):
                collecting = threading.Thread(target=lambda: monitor.ingest([listing("torid", 370)], 1))
                collecting.start(); collecting.join(0.5)
            self.assertFalse(collecting.is_alive(), "The collector waited for Discord network I/O")
            self.assertEqual(len(monitor.state()["offers"]), 2)
        finally:
            release.set(); sending.join(3)

    def test_429_is_honored_and_cannot_be_bypassed_by_wakeup(self):
        requests = []
        now = [1000.0]
        def handler(request):
            requests.append(request)
            return httpx.Response(429, json={"retry_after": 7.5}) if len(requests) == 1 else httpx.Response(204)
        monitor = self.make_monitor(handler); self.enable(monitor)
        monitor.discord.clock = lambda: now[0]
        monitor.discord.test_message()
        self.assertEqual(monitor.discord.run_once(), 7.5)
        now[0] += 1
        monitor.discord.wake.set(); monitor.discord.run_once()
        self.assertEqual(len(requests), 1)
        now[0] += 6.5
        monitor.discord.run_once()
        self.assertEqual(len(requests), 2)
        self.assertEqual(monitor.state()["discord"]["sent_count"], 1)

    def test_response_cooldown_is_respected_between_two_messages(self):
        requests = []; now = [1000.0]
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(204, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "3.2"}))
        self.enable(monitor); monitor.discord.clock = lambda: now[0]
        monitor.discord.test_message(); monitor.discord.test_message()
        self.assertEqual(monitor.discord.run_once(), 3.2)
        monitor.discord.run_once(); self.assertEqual(len(requests), 1)
        now[0] += 3.2; monitor.discord.run_once(); self.assertEqual(len(requests), 2)

    def test_persisted_webhook_is_never_returned_by_state_or_configuration_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discord.json"
            monitor = self.make_monitor(config_path=path); self.enable(monitor)
            self.assertEqual(json.loads(path.read_text())["webhook_url"], HOOK)
            self.assertNotIn(HOOK, json.dumps(monitor.state()))
            monitor.discord.configure({"enabled": False})
            self.assertTrue(monitor.state()["discord"]["configured"])
            self.assertFalse(monitor.state()["discord"]["enabled"])
            monitor.discord.configure({"clear": True})
            self.assertFalse(monitor.state()["discord"]["configured"])
            self.assertNotIn(HOOK, path.read_text())

    def test_disabled_queue_stays_paused_and_changing_channel_cancels_old_pending(self):
        requests = []
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(204)); self.enable(monitor)
        monitor.discord.test_message()
        monitor.discord.configure({"enabled": False}); monitor.discord.run_once()
        self.assertEqual(requests, [])
        self.assertEqual(monitor.state()["discord"]["pending_count"], 1)
        monitor.discord.configure({"webhook_url": OTHER_HOOK, "enabled": True})
        self.assertEqual(monitor.state()["discord"]["pending_count"], 0)

    def test_invalid_webhook_refusal_is_sanitized_and_stops_sending(self):
        requests = []
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(404, text=HOOK))
        self.enable(monitor); monitor.discord.test_message(); monitor.discord.run_once(); monitor.discord.run_once()
        self.assertEqual(len(requests), 1)
        state = monitor.state()["discord"]
        self.assertEqual(state["phase"], "error")
        self.assertEqual(state["failed_count"], 1)
        self.assertNotIn(HOOK, json.dumps(state))

    def test_outbox_and_private_configuration_survive_restart_without_realerting(self):
        requests = []
        with tempfile.TemporaryDirectory() as directory:
            path, config = Path(directory) / "live.sqlite3", Path(directory) / "discord.json"
            old = Monitor(Settings(), path, client=Stub(), discord_config_path=config)
            self.enable(old); old.ingest([], 1)
            with contextlib.redirect_stdout(io.StringIO()):
                old.ingest([listing()], 1)
            old.close()
            current = Monitor(Settings(), path, client=Stub(), discord_config_path=config,
                              discord_transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(204)))
            try:
                self.assertTrue(current.state()["discord"]["enabled"])
                self.assertEqual(current.state()["discord"]["pending_count"], 1)
                self.assertEqual(current.ingest([listing()], 1), 0)
                current.discord.run_once()
                self.assertEqual(len(requests), 1)
                self.assertEqual(current.state()["discord"]["sent_count"], 1)
            finally:
                current.close()

    def test_transient_failure_is_retried_and_never_exposes_the_webhook(self):
        requests = []; now = [1000.0]
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ConnectError(HOOK, request=request)
            return httpx.Response(204)
        monitor = self.make_monitor(handler); self.enable(monitor)
        monitor.discord.clock = lambda: now[0]
        monitor.discord.test_message(); delay = monitor.discord.run_once()
        self.assertNotIn(HOOK, json.dumps(monitor.state()))
        self.assertEqual(monitor.state()["discord"]["pending_count"], 1)
        now[0] += delay
        monitor.discord.run_once()
        self.assertEqual(monitor.state()["discord"]["sent_count"], 1)

    def test_redirect_never_forwards_webhook_or_payload_to_another_destination(self):
        requests = []
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(302, headers={"Location": "https://example.invalid"}))
        self.enable(monitor); monitor.discord.test_message(); monitor.discord.run_once()
        self.assertEqual(len(requests), 1)
        self.assertEqual(monitor.state()["discord"]["phase"], "error")

    def test_demo_is_explicitly_marked_and_old_queue_expires(self):
        requests = []; now = [1000.0]
        monitor = self.make_monitor(lambda r: requests.append(r) or httpx.Response(204), demo=True)
        self.enable(monitor); monitor.discord.clock = lambda: now[0]
        monitor.ingest([], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.ingest([listing()], 1)
        monitor.discord.run_once()
        self.assertIn("[SIMULATION]", json.loads(requests[0].content)["content"])
        monitor.discord.test_message(); now[0] += 601; monitor.discord.run_once()
        self.assertEqual(monitor.state()["discord"]["expired_count"], 1)
        self.assertEqual(len(requests), 1)
