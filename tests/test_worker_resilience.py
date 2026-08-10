import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from backend.app.worker import worker_loop


class FakeWorkerService:
    def __init__(self) -> None:
        self.calls = 0

    def claim_next_run(self):
        self.calls += 1
        if self.calls == 1:
            raise OperationalError("SELECT 1", {}, RuntimeError("SSL connection has been closed unexpectedly"))
        return None


class IdleWorkerService:
    """Never has work, so every poll takes the empty-queue path."""

    def __init__(self) -> None:
        self.calls = 0

    def claim_next_run(self):
        self.calls += 1
        return None


class ClaimThenIdleService:
    """One claimable run, then an empty queue forever."""

    def __init__(self, run) -> None:
        self.run = run
        self.calls = 0

    def claim_next_run(self):
        self.calls += 1
        return self.run if self.calls == 1 else None

    def process_claimed_run(self, run):
        return "completed"


class WorkerResilienceTests(unittest.TestCase):
    def test_claim_next_run_transient_sql_error_does_not_kill_loop(self) -> None:
        service = FakeWorkerService()
        settings = SimpleNamespace(queue_poll_seconds=0, queue_poll_max_seconds=0)

        with (
            patch("backend.app.worker.get_settings", return_value=settings),
            patch("backend.app.worker.get_run_service", return_value=service),
            patch("backend.app.worker.time.sleep", side_effect=[None, StopIteration()]),
        ):
            with self.assertRaises(StopIteration):
                worker_loop("worker-1")

        self.assertEqual(service.calls, 2)


class WorkerIdleBackoffTests(unittest.TestCase):
    def test_empty_queue_backs_off_up_to_the_ceiling(self) -> None:
        service = IdleWorkerService()
        settings = SimpleNamespace(queue_poll_seconds=2, queue_poll_max_seconds=15)
        slept: list[float] = []

        def record(seconds):
            slept.append(seconds)
            if len(slept) == 5:
                raise StopIteration()

        with (
            patch("backend.app.worker.get_settings", return_value=settings),
            patch("backend.app.worker.get_run_service", return_value=service),
            patch("backend.app.worker.time.sleep", side_effect=record),
        ):
            with self.assertRaises(StopIteration):
                worker_loop("worker-1")

        # Doubles from the 2s base and clamps at the 15s ceiling rather than
        # running away — 16 would overshoot it.
        self.assertEqual(slept, [2, 4, 8, 15, 15])

    def test_claiming_a_run_resets_the_backoff(self) -> None:
        service = ClaimThenIdleService(SimpleNamespace(id="r1", user_id="u1", project=None, keyword="k"))
        settings = SimpleNamespace(queue_poll_seconds=2, queue_poll_max_seconds=15)
        slept: list[float] = []

        def record(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                raise StopIteration()

        with (
            patch("backend.app.worker.get_settings", return_value=settings),
            patch("backend.app.worker.get_run_service", return_value=service),
            patch("backend.app.worker.time.sleep", side_effect=record),
        ):
            with self.assertRaises(StopIteration):
                worker_loop("worker-1")

        # The first poll claimed work, so the idle delay that follows starts over
        # at the base instead of carrying a stale backoff into a busy period.
        self.assertEqual(slept, [2, 4])


if __name__ == "__main__":
    unittest.main()
