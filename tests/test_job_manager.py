"""Job manager: states, progress, cancellation, concurrency limit and recovery after a restart (no FastAPI needed)."""
import tempfile
import threading
import time
import unittest

from app.services import job_manager as jm


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make(runner, max_concurrent=2, state=None):
    state = state or tempfile.mkdtemp()
    return jm.JobManager(state, state + "/cache", max_concurrent, runner), state


def quick_runner(job, report, cache_dir):
    report("Translating", 40)
    report("Writing", 90)
    return {"critical": 1, "moderate": 2, "documentScore": 95.5, "reviewRequired": True,
            "files": {"output": "o.docx", "review": "r.docx", "report": None, "log": "l.json"}}


class JobManagerTests(unittest.TestCase):
    def test_a_job_runs_to_done_and_records_its_result(self):
        manager, _ = make(quick_runner)
        job, created = manager.submit("a1", "in.pdf", "out", {"language": "ru"})
        self.assertTrue(created)
        self.assertTrue(wait_for(lambda: job.status == jm.DONE))
        self.assertEqual((job.progress, job.stage, job.critical, job.moderate), (100, "Done", 1, 2))
        self.assertEqual(job.document_score, 95.5)
        self.assertTrue(job.review_required)
        self.assertEqual(job.files, {"output": "o.docx", "review": "r.docx", "log": "l.json"})     # empty entries dropped
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)

    def test_submitting_the_same_id_again_does_not_start_a_second_run(self):
        calls = []
        manager, _ = make(lambda job, report, cache: calls.append(1) or {})
        first, created_first = manager.submit("same", "in.pdf", "out", {})
        second, created_second = manager.submit("same", "in.pdf", "out", {})
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertIs(first, second)
        wait_for(lambda: first.status == jm.DONE)
        self.assertEqual(len(calls), 1)

    def test_a_failing_job_is_marked_failed_with_its_message_and_the_worker_survives(self):
        def runner(job, report, cache):
            if job.job_id == "bad":
                raise RuntimeError("STOPPED: the translation service rejected the request")
            return {}
        manager, _ = make(runner, max_concurrent=1)
        bad, _ = manager.submit("bad", "in.pdf", "out", {})
        good, _ = manager.submit("good", "in.pdf", "out", {})
        self.assertTrue(wait_for(lambda: bad.status == jm.FAILED and good.status == jm.DONE))
        self.assertIn("rejected the request", bad.error)

    def test_progress_never_goes_backwards_and_stops_below_100_until_done(self):
        seen = []

        def runner(job, report, cache):
            for stage, pct in (("Extracting", 5), ("Translating", 50), ("Translating", 30), ("Writing", 200)):
                report(stage, pct)
                seen.append(job.progress)
            return {}
        manager, _ = make(runner)
        job, _ = manager.submit("p", "in.pdf", "out", {})
        wait_for(lambda: job.status == jm.DONE)
        self.assertEqual(seen, [5, 50, 50, 99])
        self.assertEqual(job.progress, 100)

    def test_a_queued_job_can_be_cancelled_before_it_starts(self):
        gate = threading.Event()
        manager, _ = make(lambda job, report, cache: gate.wait(5) and {}, max_concurrent=1)
        running, _ = manager.submit("first", "in.pdf", "out", {})
        wait_for(lambda: running.status == jm.RUNNING)
        queued, _ = manager.submit("second", "in.pdf", "out", {})
        job, accepted = manager.cancel("second")
        self.assertTrue(accepted)
        self.assertEqual(queued.status, jm.CANCELLED)
        gate.set()
        wait_for(lambda: running.status == jm.DONE)

    def test_a_running_job_stops_when_its_runner_sees_the_cancel_flag(self):
        started = threading.Event()

        def runner(job, report, cache):
            started.set()
            while not job.cancel_requested:
                time.sleep(0.01)
            raise RuntimeError("cancelled by the pipeline")
        manager, _ = make(runner)
        job, _ = manager.submit("c", "in.pdf", "out", {})
        started.wait(2)
        _, accepted = manager.cancel("c")
        self.assertTrue(accepted)
        self.assertTrue(wait_for(lambda: job.status == jm.CANCELLED))
        self.assertIsNone(job.error)

    def test_cancelling_a_finished_job_is_refused(self):
        manager, _ = make(quick_runner)
        job, _ = manager.submit("done", "in.pdf", "out", {})
        wait_for(lambda: job.status == jm.DONE)
        self.assertFalse(manager.cancel("done")[1])

    def test_no_more_jobs_run_at_once_than_allowed(self):
        lock, state = threading.Lock(), {"now": 0, "peak": 0}

        def runner(job, report, cache):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.1)
            with lock:
                state["now"] -= 1
            return {}
        manager, _ = make(runner, max_concurrent=2)
        jobs = [manager.submit(f"j{n}", "in.pdf", "out", {})[0] for n in range(6)]
        self.assertTrue(wait_for(lambda: all(j.status == jm.DONE for j in jobs), timeout=10))
        self.assertEqual(state["peak"], 2)

    def test_after_a_restart_finished_jobs_are_kept_and_unfinished_ones_fail(self):
        gate = threading.Event()
        manager, state = make(lambda job, report, cache: gate.wait(5) and {}, max_concurrent=1)
        done_manager_job, _ = manager.submit("running", "in.pdf", "out", {})
        wait_for(lambda: done_manager_job.status == jm.RUNNING)
        manager.submit("waiting", "in.pdf", "out", {})
        finished = jm.Job("old", "in.pdf", "out", {}, status=jm.DONE, progress=100)
        manager._save(finished)
        gate.set()
        manager.shutdown()

        fresh, _ = make(quick_runner, state=state)
        self.assertEqual(fresh.recover(), 3)
        self.assertEqual(fresh.get("old").status, jm.DONE)
        for job_id in ("running", "waiting"):
            job = fresh.get(job_id)
            self.assertIn(job.status, (jm.FAILED, jm.DONE))      # the first manager's worker may have finished it
        stuck = jm.Job("stuck", "in.pdf", "out", {}, status=jm.RUNNING)
        fresh._save(stuck)
        again, _ = make(quick_runner, state=state)
        again.recover()
        self.assertEqual(again.get("stuck").status, jm.FAILED)
        self.assertEqual(again.get("stuck").error, jm.RESTARTED)

    def test_list_filters_by_status_and_counts_running_and_queued(self):
        gate = threading.Event()
        manager, _ = make(lambda job, report, cache: gate.wait(5) and {}, max_concurrent=1)
        first, _ = manager.submit("l1", "in.pdf", "out", {})
        wait_for(lambda: first.status == jm.RUNNING)
        manager.submit("l2", "in.pdf", "out", {})
        self.assertEqual(manager.counts(), {"running": 1, "queued": 1})
        self.assertEqual([j.job_id for j in manager.list(jm.QUEUED)], ["l2"])
        gate.set()


if __name__ == "__main__":
    unittest.main()
