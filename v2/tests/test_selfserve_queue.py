"""Queue-drain safety (2026-07-18 incident audit): the Action runner drains ONLY status=="queued"
requests. Previously list_pending_jobs() selected every request without a result, ignoring status —
so an owner-cancelled request was still a standing spend landmine that any future run would bill.
Pure, no network: the store reads are mocked. Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import selfserve


def _store(requests: dict, results: dict):
    """Fake _listdir/_read over an in-memory store. requests: fname -> record dict;
    results: job_id -> result dict."""
    def listdir(path, include_dirs=False):
        return sorted(requests) if path == selfserve.REQUESTS_DIR else []
    def read(path):
        if path.startswith(f"{selfserve.RESULTS_DIR}/"):
            job_id = path.split("/")[1]
            return json.dumps(results[job_id]) if job_id in results else None
        fname = path.split("/")[-1]
        return json.dumps(requests[fname]) if fname in requests else None
    return listdir, read


class QueueDrain(unittest.TestCase):
    def _pending(self, requests, results=None):
        listdir, read = _store(requests, results or {})
        with mock.patch.object(selfserve, "_listdir", listdir), \
             mock.patch.object(selfserve, "_read", read):
            return selfserve.list_pending_jobs()

    def test_queued_without_result_is_drained_oldest_first(self):
        reqs = {"b__20260702-000000.json": {"job_id": "b__20260702-000000", "status": "queued"},
                "a__20260701-000000.json": {"job_id": "a__20260701-000000", "status": "queued"}}
        out = self._pending(reqs)
        self.assertEqual([r["job_id"] for r in out],
                         ["a__20260701-000000", "b__20260702-000000"])

    def test_cancelled_is_never_drained(self):
        """The incident case: an owner-cancelled request must be dead to the runner, forever."""
        reqs = {"x__20260717-022651.json": {"job_id": "x__20260717-022651",
                                            "status": "cancelled"}}
        self.assertEqual(self._pending(reqs), [])

    def test_rejected_and_unknown_statuses_are_never_drained(self):
        reqs = {"r.json": {"job_id": "r", "status": "rejected"},
                "u.json": {"job_id": "u", "status": "weird-future-status"},
                "m.json": {"job_id": "m"}}                     # missing status: not queued -> dead
        self.assertEqual(self._pending(reqs), [])

    def test_already_processed_is_skipped_regardless_of_status(self):
        reqs = {"d.json": {"job_id": "d", "status": "queued"}}
        out = self._pending(reqs, results={"d": {"status": "done"}})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
