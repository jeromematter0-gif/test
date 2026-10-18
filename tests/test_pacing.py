import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from riven_sniper.core import ApiFailure, Monitor, PollPacing, Settings


class PollPacingTests(unittest.TestCase):
    def test_success_between_quota_responses_does_not_reset_slowdown(self):
        pacing = PollPacing(1)
        self.assertEqual(pacing.quota(retry_after=7, now=0), (7, "server"))
        pacing.success(now=7)
        self.assertEqual(pacing.effective_interval, 2)

        self.assertEqual(pacing.quota(retry_after=None, now=9), (30, "quota"))
        self.assertEqual(pacing.effective_interval, 4)
        self.assertEqual(pacing.quota_hits, 2)
        self.assertEqual(pacing.quota_level, 2)

    def test_server_delay_can_exceed_both_local_caps(self):
        pacing = PollPacing(1)
        delay, reason = pacing.quota(retry_after=3600, now=0)
        self.assertEqual(delay, 3600)
        self.assertEqual(reason, "server")
        self.assertEqual(pacing.effective_interval, 2)

    def test_explicit_zero_server_delay_still_observes_effective_cadence(self):
        pacing = PollPacing(1)
        self.assertEqual(pacing.quota(retry_after=0, now=0), (2, "quota"))

    def test_missing_server_header_uses_progressive_bounded_local_pauses(self):
        pacing = PollPacing(1)
        delays = [pacing.quota(retry_after=None, now=i)[0] for i in range(8)]
        self.assertEqual(delays, [15, 30, 60, 120, 120, 120, 120, 120])
        self.assertEqual(pacing.effective_interval, 60)
        self.assertEqual(pacing.quota_hits, 8)

    def test_many_quick_successes_do_not_prematurely_restore_speed(self):
        pacing = PollPacing(1)
        pacing.quota(retry_after=0, now=0)
        for now in range(60):
            pacing.success(now=now)
        self.assertEqual(pacing.effective_interval, 2)

        pacing.success(now=120)
        self.assertAlmostEqual(pacing.effective_interval, 1.8)
        self.assertEqual(pacing.quota_level, 0)
        self.assertEqual(pacing.quota_hits, 1)
        pacing.success(now=121)
        self.assertAlmostEqual(pacing.effective_interval, 1.8)

    def test_elapsed_time_alone_is_insufficient_for_recovery(self):
        pacing = PollPacing(1)
        pacing.quota(retry_after=0, now=0)
        pacing.success(now=5)
        pacing.success(now=125)
        self.assertEqual(pacing.effective_interval, 2)

        for _ in range(58):
            pacing.success(now=125)
        self.assertAlmostEqual(pacing.effective_interval, 1.8)

    def test_new_quota_resets_stability_window_and_success_count(self):
        pacing = PollPacing(1)
        pacing.quota(retry_after=0, now=0)
        for now in range(60):
            pacing.success(now=now)
        pacing.quota(retry_after=0, now=60)
        for now in range(61, 121):
            pacing.success(now=now)
        self.assertEqual(pacing.effective_interval, 4)

        pacing.success(now=181)
        self.assertAlmostEqual(pacing.effective_interval, 3.6)
        self.assertEqual(pacing.quota_hits, 2)

    def test_recovery_never_goes_faster_than_user_configured_interval(self):
        pacing = PollPacing(3, learned_interval=3.1)
        for now in range(60):
            pacing.success(now=now * 3)
        self.assertEqual(pacing.effective_interval, 3)

    def test_learned_interval_is_bounded_and_invalid_values_are_ignored(self):
        self.assertEqual(PollPacing(1, learned_interval=3.5).effective_interval, 3.5)
        self.assertEqual(PollPacing(1, learned_interval=600).effective_interval, 60)
        for learned in [None, False, True, 0, -1, 0.5, float("nan"), float("inf"), -float("inf"), "invalid", {}, []]:
            with self.subTest(learned=learned):
                self.assertEqual(PollPacing(1, learned_interval=learned).effective_interval, 1)

    def test_custom_slow_base_is_never_limited_to_sixty_seconds(self):
        pacing = PollPacing(90, learned_interval=120)
        self.assertEqual(pacing.effective_interval, 90)
        self.assertEqual(pacing.quota(retry_after=None, now=0), (90, "quota"))
        self.assertEqual(pacing.quota(retry_after=10, now=90), (90, "quota"))


class PacingSchedulerTests(unittest.TestCase):
    def run_scenario(self, outcomes):
        now = [0.0]
        calls, waits = [], []

        class Client:
            def search(self, source="recent"):
                index = len(calls)
                calls.append((source, now[0]))
                outcome = outcomes[index]
                if isinstance(outcome, ApiFailure):
                    raise outcome
                now[0] += outcome
                return [], outcome * 1000

            def close(self):
                pass

        class Wait:
            def is_set(self):
                return len(calls) >= len(outcomes)

            def wait(self, delay=None):
                waits.append((delay, monitor.state()["status"].copy()))
                now[0] += delay or 0

            def set(self):
                pass

        monitor = Monitor(Settings(), ":memory:", client=Client())
        self.addCleanup(monitor.close)
        monitor.stop_event = Wait()
        with patch("riven_sniper.core.time.monotonic", side_effect=lambda: now[0]), contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        return calls, waits, monitor.state()["status"]

    def test_intermittent_quotas_slow_all_sources_and_publish_real_pacing(self):
        calls, waits, status = self.run_scenario([
            ApiFailure("quota", retry_after=7, kind="quota", http_status=429),
            0.2,
            ApiFailure("quota", retry_after=17, kind="quota", http_status=429),
            0.2,
            0.2,
        ])
        self.assertEqual([source for source, _ in calls], list(Settings().sources())[:5])
        for (_, actual), expected in zip(calls, [0, 7, 9, 26, 30]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(waits[0][1]["server_retry_after_seconds"], 7)
        self.assertEqual(waits[0][1]["backoff_reason"], "server")
        self.assertEqual(waits[2][1]["server_retry_after_seconds"], 17)
        self.assertEqual(waits[2][1]["backoff_reason"], "server")
        self.assertEqual(status["effective_interval_seconds"], 4)
        self.assertEqual(status["rate_limit_count"], 2)
        self.assertEqual(status["successful_polls"], 3)
        self.assertEqual(status["failed_polls"], 2)

    def test_missing_retry_header_retains_quota_progression_across_success(self):
        calls, waits, status = self.run_scenario([
            ApiFailure("quota", kind="quota", http_status=429),
            0.1,
            ApiFailure("quota", kind="quota", http_status=429),
            0.1,
        ])
        for (_, actual), expected in zip(calls, [0, 15, 17, 47]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(waits[0][0], 15)
        self.assertEqual(waits[2][0], 30)
        self.assertIsNone(waits[2][1]["server_retry_after_seconds"])
        self.assertEqual(waits[2][1]["backoff_reason"], "quota")
        self.assertEqual(status["effective_interval_seconds"], 4)

    def test_slow_response_already_satisfies_adapted_start_to_start_spacing(self):
        calls, _, status = self.run_scenario([
            ApiFailure("quota", retry_after=7, kind="quota", http_status=429),
            3,
            0.1,
        ])
        self.assertEqual(calls[1][1], 7)
        self.assertEqual(calls[2][1], 10)
        self.assertEqual(status["effective_interval_seconds"], 2)


class PacingRestartTests(unittest.TestCase):
    def run_restart(self, shutdown_during_wait=False):
        now = [1000.0]
        calls, waits = [], []

        class Client:
            def search(self, source="recent"):
                calls.append((source, now[0]))
                return [], 0

            def close(self):
                pass

        class Wait:
            stopped = False

            def is_set(self):
                return self.stopped or bool(calls)

            def wait(self, delay=None):
                waits.append((delay, len(calls), restored.state()["status"].copy()))
                if shutdown_during_wait:
                    self.stopped = True
                else:
                    now[0] += delay or 0

            def set(self):
                self.stopped = True

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "live.sqlite3"
            original = Monitor(Settings(), database, client=Client())
            try:
                original.pacing.quota(retry_after=300, now=0)
                original.pacing.quota(retry_after=300, now=1)
                original.save_pacing(1200, reason="server", retry_after=300)
            finally:
                original.close()

            restored = Monitor(Settings(), database, client=Client())
            try:
                self.assertEqual(restored.state()["status"]["effective_interval_seconds"], 4)
                self.assertEqual(restored.pacing_deadline, 1200)
                restored.stop_event = Wait()
                with patch("riven_sniper.core.time.time", side_effect=lambda: now[0]), patch("riven_sniper.core.time.monotonic", side_effect=lambda: now[0]), contextlib.redirect_stdout(io.StringIO()):
                    restored.run()
                persisted = dict(restored.db.execute("SELECT * FROM collector_pacing WHERE singleton=1").fetchone())
                return calls, waits, restored.state()["status"], persisted
            finally:
                restored.close()

    def test_restart_preserves_learned_speed_and_waits_before_first_request(self):
        calls, waits, status, persisted = self.run_restart()
        self.assertEqual(calls, [("recent", 1200)])
        self.assertEqual(waits[0][0], 200)
        self.assertEqual(waits[0][1], 0)
        self.assertEqual(waits[0][2]["phase"], "backoff")
        self.assertEqual(waits[0][2]["backoff_reason"], "server")
        self.assertEqual(waits[0][2]["server_retry_after_seconds"], 300)
        self.assertEqual(status["effective_interval_seconds"], 4)
        self.assertEqual(persisted["interval_seconds"], 4)
        self.assertIsNone(persisted["cooldown_until"])

    def test_shutdown_during_restored_cooldown_does_not_contact_marketplace(self):
        calls, waits, status, persisted = self.run_restart(shutdown_during_wait=True)
        self.assertEqual(calls, [])
        self.assertEqual(waits[0][0], 200)
        self.assertEqual(status["polls"], 0)
        self.assertEqual(persisted["cooldown_until"], 1200)


if __name__ == "__main__":
    unittest.main()
