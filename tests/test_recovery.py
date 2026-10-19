import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from riven_sniper.core import ApiFailure, Monitor, PollPacing, Settings


class RecoveryTests(unittest.TestCase):
    def test_reported_46_second_interval_recovers_after_stability_not_sixty_polls(self):
        pacing = PollPacing(1, learned_interval=46.7)
        for now in (0, 46.7, 93.4):
            pacing.success(now)
            self.assertEqual(pacing.effective_interval, 46.7)
        pacing.success(140.1)
        self.assertEqual(pacing.effective_interval, 23.35)
        self.assertEqual(pacing.stable_successes, 0)
        pacing.success(163.45)
        self.assertEqual(pacing.effective_interval, 23.35)

    def test_idle_time_cannot_substitute_for_three_actual_successes(self):
        pacing = PollPacing(1, learned_interval=50)
        pacing.success(0)
        pacing.success(3600)
        self.assertEqual(pacing.effective_interval, 50)
        pacing.success(3650)
        self.assertEqual(pacing.effective_interval, 25)

    def test_network_failure_resets_the_stability_window(self):
        pacing = PollPacing(1, learned_interval=40)
        pacing.success(0)
        pacing.success(40)
        pacing.failure()
        for now in (200, 240, 280):
            pacing.success(now)
        self.assertEqual(pacing.effective_interval, 40)
        pacing.success(320)
        self.assertEqual(pacing.effective_interval, 20)
        self.assertEqual(pacing.quota_hits, 0)

    def test_recovery_progress_reports_both_requirements(self):
        pacing = PollPacing(1, learned_interval=46.7)
        pacing.success(100)
        pacing.success(146.7)
        progress = pacing.recovery_status(150)
        self.assertTrue(progress['active'])
        self.assertEqual(progress['successes'], 2)
        self.assertEqual(progress['required_successes'], 3)
        self.assertEqual(progress['stable_seconds'], 50)
        self.assertEqual(progress['required_seconds'], 120)
        self.assertEqual(progress['next_interval_seconds'], 23.35)
        pacing.quota(60, 150)
        progress = pacing.recovery_status(180)
        self.assertEqual(progress['successes'], 0)
        self.assertEqual(progress['stable_seconds'], 0)
        self.assertFalse(PollPacing(1).recovery_status(180)['active'])

    def test_persisted_46_second_interval_recovers_without_a_reset_or_extra_requests(self):
        now, calls = [0.0], []

        class Client:
            def search(self, source='recent'):
                calls.append((now[0], source))
                return [], 0

            def close(self):
                pass

        class Wait:
            def is_set(self):
                return restored.pacing.effective_interval == 1 or len(calls) >= 300

            def wait(self, delay=None):
                now[0] += delay or 0

            def set(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'live.sqlite3'
            original = Monitor(Settings(), database, client=Client())
            original.pacing.effective_interval = 46.7
            original.save_pacing()
            original.close()
            restored = Monitor(Settings(), database, client=Client())
            try:
                self.assertEqual(restored.pacing.effective_interval, 46.7)
                restored.stop_event = Wait()
                with patch('riven_sniper.core.time.monotonic', side_effect=lambda: now[0]):
                    restored.run()
                self.assertEqual(restored.pacing.effective_interval, 1)
                self.assertLess(now[0], 15 * 60)
                self.assertEqual([round(t, 1) for t, _ in calls[:4]], [0, 46.7, 93.4, 140.1])
                self.assertTrue(all(b[0] - a[0] >= 0.999 for a, b in zip(calls, calls[1:])))
                self.assertEqual(len(calls), restored.status['polls'])
                saved = restored.db.execute('SELECT interval_seconds FROM collector_pacing').fetchone()[0]
                self.assertEqual(saved, 1)
            finally:
                restored.close()

    def test_scheduler_network_error_prevents_recovery_on_its_next_success(self):
        now, calls = [0.0], []

        class Client:
            def search(self, source='recent'):
                calls.append(now[0])
                if len(calls) == 3:
                    raise ApiFailure('Network timeout', kind='timeout')
                return [], 0

            def close(self):
                pass

        class Wait:
            def is_set(self):
                return len(calls) >= 4

            def wait(self, delay=None):
                now[0] += delay or 0

            def set(self):
                pass

        monitor = Monitor(Settings(), ':memory:', client=Client())
        self.addCleanup(monitor.close)
        monitor.pacing.effective_interval = 40
        monitor.stop_event = Wait()
        with patch('riven_sniper.core.time.monotonic', side_effect=lambda: now[0]), contextlib.redirect_stdout(io.StringIO()):
            monitor.run()
        self.assertEqual(calls, [0, 40, 80, 120])
        self.assertEqual(monitor.pacing.effective_interval, 40)
        self.assertEqual(monitor.pacing.stable_successes, 1)

    def test_console_prints_local_progress_even_without_detections_and_never_calls_api(self):
        class Client:
            def search(self, source='recent'):
                raise AssertionError('Heartbeat must not query the API')

            def close(self):
                pass

        class Wait:
            stopped = False
            delays = []

            def is_set(self):
                return self.stopped

            def wait(self, delay=None):
                self.delays.append(delay)
                self.stopped = True

            def set(self):
                self.stopped = True

        monitor = Monitor(Settings(), ':memory:', client=Client())
        self.addCleanup(monitor.close)
        monitor.stop_event = Wait()
        monitor.status.update(phase='watching', successful_polls=43, failed_polls=3,
                              effective_interval_seconds=46.7, message='secret webhook must not appear')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            monitor.console_run()
        self.assertIn('Surveillance active', output.getvalue())
        self.assertIn('43 réponses valides, 3 échecs', output.getvalue())
        self.assertIn('intervalle 46.7 s', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(monitor.stop_event.delays, [30])
        self.assertEqual(monitor.status['polls'], 0)


if __name__ == '__main__':
    unittest.main()
