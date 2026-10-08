"""HTTP contract of the extraction service, with a stand-in runner (needs fastapi and httpx; skipped without them)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from app.core.config import Settings
    from app.main import create_app
    AVAILABLE = True
except ImportError:                                           # pragma: no cover
    AVAILABLE = False

KEY = {"X-API-Key": "secret"}


def fake_runner(job, report, cache_dir):
    report("Translating", 50)
    return {"critical": 0, "moderate": 3, "documentScore": 96.0, "reviewRequired": True,
            "files": {"output": str(Path(job.output_dir) / "o.docx"), "review": str(Path(job.output_dir) / "r.docx")}}


@unittest.skipUnless(AVAILABLE, "fastapi / httpx are not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.root = (base / "files").resolve()
        self.root.mkdir()
        self.src = self.root / "in.docx"
        self.src.write_bytes(b"x")
        settings = Settings(files_root=self.root, state_dir=base / "state", api_key="secret", max_concurrent_jobs=1)
        self.gate = threading.Event()
        self.gate.set()

        def runner(job, report, cache_dir):
            self.gate.wait(5)
            return fake_runner(job, report, cache_dir)
        self.client = TestClient(create_app(settings, runner))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def body(self, job_id="j1", **extra):
        return {"jobId": job_id, "inputPath": str(self.src), "outputDir": str(self.root / "out" / job_id), **extra}

    def wait_done(self, job_id, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            data = self.client.get(f"/jobs/{job_id}", headers=KEY).json()
            if data["status"] in ("Done", "Failed", "Cancelled"):
                return data
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_every_job_endpoint_needs_the_api_key(self):
        self.assertEqual(self.client.post("/jobs", json=self.body()).status_code, 401)
        self.assertEqual(self.client.get("/jobs/j1", headers={"X-API-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/jobs").json(), {"code": "UNAUTHORIZED", "message": "Missing or invalid API key."})

    def test_start_then_read_progress_and_result_in_camel_case(self):
        started = self.client.post("/jobs", json=self.body(options={"language": "ru", "verify": False}), headers=KEY)
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["jobId"], "j1")
        final = self.wait_done("j1")
        self.assertEqual(final["status"], "Done")
        self.assertEqual((final["progress"], final["moderate"], final["documentScore"], final["reviewRequired"]), (100, 3, 96.0, True))
        self.assertTrue(final["files"]["output"].endswith("o.docx"))
        self.assertIn("createdAt", final)

    def test_sending_the_same_job_again_returns_it_instead_of_starting_another(self):
        first = self.client.post("/jobs", json=self.body(), headers=KEY)
        again = self.client.post("/jobs", json=self.body(), headers=KEY)
        self.assertEqual((first.status_code, again.status_code), (202, 200))
        other = self.root / "other.docx"
        other.write_bytes(b"x")
        clash = self.client.post("/jobs", json={**self.body(), "inputPath": str(other)}, headers=KEY)
        self.assertEqual((clash.status_code, clash.json()["code"]), (409, "JOB_EXISTS"))

    def test_paths_outside_the_files_folder_and_bad_files_are_refused(self):
        outside = self.client.post("/jobs", json={**self.body("o1"), "inputPath": str(self.root.parent / "secret.docx")}, headers=KEY)
        self.assertEqual((outside.status_code, outside.json()["code"]), (400, "PATH_OUTSIDE_ROOT"))
        traversal = self.client.post("/jobs", json={**self.body("o2"), "outputDir": str(self.root / ".." / "elsewhere")}, headers=KEY)
        self.assertEqual(traversal.json()["code"], "PATH_OUTSIDE_ROOT")
        missing = self.client.post("/jobs", json={**self.body("o3"), "inputPath": str(self.root / "none.docx")}, headers=KEY)
        self.assertEqual(missing.json()["code"], "INPUT_NOT_FOUND")
        text = self.root / "notes.txt"
        text.write_bytes(b"x")
        wrong = self.client.post("/jobs", json={**self.body("o4"), "inputPath": str(text)}, headers=KEY)
        self.assertEqual(wrong.json()["code"], "UNSUPPORTED_FILE_TYPE")

    def test_invalid_requests_use_the_same_error_shape(self):
        bad = self.client.post("/jobs", json={"jobId": "has spaces!", "inputPath": "x", "outputDir": "y"}, headers=KEY)
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(self.client.get("/jobs/nope", headers=KEY).json()["code"], "JOB_NOT_FOUND")
        language = self.client.post("/jobs", json=self.body("l1", options={"language": "fr"}), headers=KEY)
        self.assertEqual(language.status_code, 422)

    def test_a_queued_job_can_be_cancelled_and_a_finished_one_cannot(self):
        self.gate.clear()
        self.client.post("/jobs", json=self.body("running"), headers=KEY)
        self.client.post("/jobs", json=self.body("waiting"), headers=KEY)
        cancelled = self.client.post("/jobs/waiting/cancel", headers=KEY)
        self.assertEqual((cancelled.status_code, cancelled.json()["status"]), (200, "Cancelled"))
        self.gate.set()
        self.wait_done("running")
        late = self.client.post("/jobs/running/cancel", headers=KEY)
        self.assertEqual((late.status_code, late.json()["code"]), (409, "JOB_FINISHED"))
        self.assertEqual(self.client.post("/jobs/ghost/cancel", headers=KEY).status_code, 404)

    def test_listing_can_be_filtered_by_status_and_health_needs_no_key(self):
        self.client.post("/jobs", json=self.body("a"), headers=KEY)
        self.wait_done("a")
        done = self.client.get("/jobs", params={"status": "Done"}, headers=KEY).json()
        self.assertEqual([j["jobId"] for j in done], ["a"])
        self.assertEqual(self.client.get("/jobs", params={"status": "Running"}, headers=KEY).json(), [])
        health = self.client.get("/health").json()
        self.assertEqual((health["status"], health["maxConcurrent"]), ("ok", 1))
        self.assertIn("openrouterKeyConfigured", health)


if __name__ == "__main__":
    unittest.main()
