"""Conflict-safe DATA-store writes: concurrent appends must not silently drop.

The analytics visit log appends from many viewer sessions at once. The plain `_write` reads the
sha then PUTs with NO retry, so two writers that read the same sha race and the loser gets a 409
and its write VANISHES — the bug that swallowed real visits (incl. a 7am one). `update_data` /
`append_data` fix that with optimistic-concurrency: a 409/422 sha conflict re-reads the fresh
content, re-applies the transform, and retries (with jittered backoff) so writers serialize.

These tests run against a faithful in-memory mock of the GitHub Contents API — sha = hash(content)
and PUT is an ATOMIC compare-and-set that 409s on a stale sha, exactly the prod backend's
optimistic lock. No network, no model.

    python -m unittest discover -s tests
"""
import hashlib
import json
import threading
import unittest
from unittest import mock

import httpx

from scout import selfserve


class _FakeGH:
    """In-memory stand-in for one file behind the GitHub Contents API. PUT is a server-side atomic
    compare-and-set: it 409s unless the caller's sha matches the live content's sha."""

    def __init__(self):
        self.content = None
        self.lock = threading.Lock()

    def _sha(self, c):
        return None if c is None else hashlib.sha256(c.encode()).hexdigest()

    def get(self):
        with self.lock:
            return self.content, self._sha(self.content)

    def put(self, text, sha):
        with self.lock:
            if sha != self._sha(self.content):
                req = httpx.Request("PUT", "https://api.github.com/x")
                raise httpx.HTTPStatusError("conflict", request=req,
                                            response=httpx.Response(409, request=req))
            self.content = text


class ConcurrentAppendTest(unittest.TestCase):
    def setUp(self):
        self.gh = _FakeGH()
        self._patches = [
            mock.patch.object(selfserve, "use_github", lambda: True),
            mock.patch.object(selfserve, "_gh_get", lambda p: self.gh.get()),
            mock.patch.object(selfserve, "_gh_put",
                                       lambda p, t, m, sha=None: self.gh.put(t, sha)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _lines(self):
        return [json.loads(l) for l in (self.gh.content or "").splitlines() if l.strip()]

    def test_no_append_is_lost_under_concurrency(self):
        n = 30
        threads = [threading.Thread(target=lambda i=i: selfserve.append_data(
            "analytics/visits.jsonl", json.dumps({"i": i}), f"v{i}")) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = {r["i"] for r in self._lines()}
        self.assertEqual(got, set(range(n)), f"lost appends: {set(range(n)) - got}")

    def test_update_data_returns_false_when_transform_aborts(self):
        # a transform that returns None must NOT write and must report no-op
        selfserve.append_data("f.jsonl", json.dumps({"_cid": "abc"}), "seed")
        wrote = selfserve.update_data("f.jsonl", lambda cur: None, "noop")
        self.assertFalse(wrote)
        self.assertEqual(len(self._lines()), 1)

    def test_concurrent_appends_plus_a_modify_all_survive(self):
        # mirrors prod: visits appending while a city-patch modifies one record in place
        selfserve.append_data("v.jsonl", json.dumps({"_cid": "target", "geo": ""}), "seed")

        def patch_geo(cur):
            lines = (cur or "").splitlines()
            for i, ln in enumerate(lines):
                rec = json.loads(ln)
                if rec.get("_cid") == "target" and not rec.get("geo"):
                    rec["geo"] = "San Jose"
                    lines[i] = json.dumps(rec)
                    return "\n".join(lines) + "\n"
            return None

        workers = [threading.Thread(target=lambda i=i: selfserve.append_data(
            "v.jsonl", json.dumps({"i": i}), f"v{i}")) for i in range(15)]
        workers.append(threading.Thread(
            target=lambda: selfserve.update_data("v.jsonl", patch_geo, "geo")))
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        recs = self._lines()
        self.assertEqual(len([r for r in recs if "i" in r]), 15)         # no appended visit lost
        self.assertEqual([r for r in recs if r.get("_cid") == "target"][0]["geo"], "San Jose")


if __name__ == "__main__":
    unittest.main()
