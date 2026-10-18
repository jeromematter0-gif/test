import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from riven_sniper.core import ApiFailure, Monitor, Settings


class PrioritySchedulerTests(unittest.TestCase):
    def run_scenario(self, *, attempts, settings=None, response=None, database=":memory:"):
        """Run the real collector against a fake clock and public API client."""
        now = [0.0]
        calls, waits = [], []

        class Client:
            def search(self, source="recent"):
                calls.append((source, now[0]))
                outcome = response(source, len(calls)) if response else 0.2
                if isinstance(outcome, ApiFailure):
                    raise outcome
                now[0] += outcome
                return [], outcome * 1000

            def close(self):
                pass

        class Wait:
            def is_set(self):
                return len(calls) >= attempts

            def wait(self, delay=None):
                waits.append((delay, monitor.state()["status"].copy()))
                now[0] += delay or 0

            def set(self):
                pass

        monitor = Monitor(settings or Settings(), database, client=Client())
        self.addCleanup(monitor.close)
        self.last_monitor = monitor
        monitor.stop_event = Wait()
        with patch("riven_sniper.core.time.monotonic", side_effect=lambda: now[0]), \
                patch("riven_sniper.core.time.time", side_effect=lambda: 2_000_000_000 + now[0]), \
                contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        return calls, waits, monitor.state()

    def test_default_mode_exposes_search_spacing_without_removing_any_weapon(self):
        settings = Settings()
        self.assertEqual(settings.collection_mode, "priority")
        self.assertEqual(settings.public()["search_gap_seconds"], 30)
        self.assertEqual(len(settings.sources()), 6)
        self.assertEqual(settings.sources()[0], "recent")
        self.assertEqual(set(settings.sources()[1:]), {
            "weapon:burston", "weapon:dual_toxocyst", "weapon:torid",
            "weapon:verglas", "weapon:magistar",
        })
        for mode in ("hybrid", "recent", "weapons"):
            with self.subTest(mode=mode):
                self.assertIsNone(Settings(collection_mode=mode).public().get("search_gap_seconds"))

    def test_recent_takes_most_slots_while_all_five_searches_eventually_initialize(self):
        calls, _, state = self.run_scenario(attempts=125)
        self.assertEqual([source for source, _ in calls[:3]], ["recent", "weapon:burston", "recent"])
        searches = [(source, started) for source, started in calls if source != "recent"]
        self.assertEqual([source for source, _ in searches], list(Settings().sources()[1:]))
        self.assertGreater(sum(source == "recent" for source, _ in calls), len(calls) * 0.9)
        self.assertTrue(all(later[1] - earlier[1] >= 30 for earlier, later in zip(searches, searches[1:])))
        self.assertTrue(all(later[1] - earlier[1] >= 0.999 for earlier, later in zip(calls, calls[1:])))
        self.assertEqual(state["status"]["warmup_remaining"], 0)
        self.assertTrue(all(source["ready"] for source in state["sources"]))

    def test_targeted_rotation_stays_fair_after_the_first_five_searches(self):
        calls, _, _ = self.run_scenario(attempts=275)
        targeted = [source for source, _ in calls if source != "recent"]
        expected = list(Settings().sources()[1:])
        self.assertEqual(targeted, expected * 2)

    def test_slow_common_interval_still_inserts_recent_between_targeted_searches(self):
        calls, _, _ = self.run_scenario(attempts=12, settings=Settings(interval_seconds=40))
        for (previous_source, previous_time), (source, started) in zip(calls, calls[1:]):
            self.assertGreaterEqual(started - previous_time, 40)
            self.assertTrue(previous_source == "recent" or source == "recent")
        self.assertEqual([source for source, _ in calls if source != "recent"],
                         list(Settings().sources()[1:]) + ["weapon:burston"])

    def test_response_time_counts_toward_search_spacing_instead_of_counting_polls(self):
        calls, _, _ = self.run_scenario(attempts=8, response=lambda source, attempt: 12)
        targeted = [(source, started) for source, started in calls if source != "recent"]
        self.assertEqual([started for source, started in targeted], [12, 48, 84])
        self.assertEqual([source for source, _ in targeted], list(Settings().sources()[1:4]))

    def test_slow_search_does_not_add_a_second_full_search_delay_after_its_response(self):
        calls, _, _ = self.run_scenario(
            attempts=4, response=lambda source, attempt: 35 if source != "recent" else 0.2)
        self.assertEqual([source for source, _ in calls], [
            "recent", "weapon:burston", "recent", "weapon:dual_toxocyst",
        ])
        self.assertAlmostEqual(calls[1][1], 1)
        self.assertAlmostEqual(calls[2][1], 36)
        self.assertAlmostEqual(calls[3][1], 37)

    def test_restored_eight_second_budget_is_retained_for_every_source(self):
        class Client:
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "live.sqlite3"
            original = Monitor(Settings(), database, client=Client())
            try:
                original.pacing.effective_interval = 8
                original.save_pacing()
            finally:
                original.close()
            calls, _, state = self.run_scenario(attempts=20, database=database)
            self.assertEqual(state["status"]["effective_interval_seconds"], 8)
            self.assertTrue(all(later[1] - earlier[1] >= 8 for earlier, later in zip(calls, calls[1:])))
            recent_starts = [started for source, started in calls if source == "recent"]
            self.assertEqual(recent_starts[:2], [0, 16])
            self.assertTrue(all(later - earlier <= 16 for earlier, later in zip(recent_starts, recent_starts[1:])))

    def test_search_quota_pauses_recent_too_and_preserves_common_adaptation(self):
        quota = ApiFailure("quota", retry_after=60, kind="quota", http_status=429)
        calls, waits, state = self.run_scenario(
            attempts=5, response=lambda source, attempt: quota if attempt == 2 else 0.2)
        self.assertEqual(calls[1][0], "weapon:burston")
        self.assertEqual(calls[2][0], "recent")
        self.assertGreaterEqual(calls[2][1] - calls[1][1], 60)
        self.assertEqual(waits[1][1]["server_retry_after_seconds"], 60)
        self.assertEqual(waits[1][1]["backoff_reason"], "server")
        self.assertEqual(state["status"]["effective_interval_seconds"], 2)
        self.assertEqual(state["status"]["rate_limit_count"], 1)
        self.assertTrue(all(later[1] - earlier[1] >= 2 for earlier, later in zip(calls[2:], calls[3:])))
        searches = [(source, started) for source, started in calls if source != "recent"]
        self.assertGreaterEqual(searches[1][1] - searches[0][1], 30)

    def test_attempt_history_keeps_last_sixty_outcomes_without_error_text_or_mutable_aliases(self):
        quota = ApiFailure("private-example-token", retry_after=60, kind="quota", http_status=429,
                           duration_ms=19.06)
        calls, _, state = self.run_scenario(
            attempts=61, response=lambda source, attempt: quota if attempt == 2 else 0.2)
        history = self.last_monitor.attempt_history()
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0]["source"], calls[1][0])
        self.assertEqual(history[0]["http_status"], 429)
        self.assertEqual(history[0]["duration_ms"], 19.06)
        self.assertEqual(history[0]["error_kind"], "quota")
        self.assertEqual(history[0]["retry_after_seconds"], 60)
        self.assertEqual(history[0]["wait_seconds"], 60)
        self.assertEqual(history[0]["effective_interval_seconds"], 2)
        self.assertEqual(history[1]["source"], "recent")
        self.assertEqual(history[1]["http_status"], 200)
        self.assertIsNone(history[1]["error_kind"])
        self.assertIsNone(history[1]["retry_after_seconds"])
        self.assertEqual(history[-1]["source"], calls[-1][0])
        self.assertNotIn("private-example-token", json.dumps(history))
        self.assertNotIn("recent_attempts", state)
        self.assertEqual(set(history[0]), {
            "started_at", "source", "http_status", "duration_ms", "error_kind",
            "retry_after_seconds", "wait_seconds", "effective_interval_seconds",
        })
        history[0]["http_status"] = 999
        history.pop()
        unchanged = self.last_monitor.attempt_history()
        self.assertEqual(len(unchanged), 60)
        self.assertEqual(unchanged[0]["http_status"], 429)


if __name__ == "__main__":
    unittest.main()
