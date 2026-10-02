"""scout/signals.py (WS3, 2026-09-29): filings trigger, a brand-new department queues, hiring is
context; dedupe, caps, state round-trip, consume. In-memory store; EDGAR and the boards are faked."""
import json
import unittest
from unittest import mock

from scout import config, selfserve, signals


class _Store:
    def __init__(self):
        self.files = {}
    def read(self, path):
        return self.files.get(path)
    def write(self, path, text, message):
        self.files[path] = text
    def update(self, path, transform, message, **kw):
        self.files[path] = transform(self.files.get(path)); return True
    def list(self, path, include_dirs=False):
        return sorted({k[len(path) + 1:].split("/")[0] for k in self.files if k.startswith(path + "/")})


FILINGS_T0 = [{"form": "10-Q", "filed": "2026-08-27", "accession": "0001-26-000190", "url": "https://www.sec.gov/a/190.htm", "index_url": "https://www.sec.gov/a/", "description": "10-Q", "items": ""}]
FILINGS_T1 = [{"form": "8-K", "filed": "2026-09-29", "accession": "0001-26-000205", "url": "https://www.sec.gov/a/205.htm", "index_url": "https://www.sec.gov/a/", "description": "Current report", "items": "2.02,9.01"}] + FILINGS_T0
BOARD_T0 = [{"id": "1", "title": "AE", "department": "Sales", "location": "SF", "updated_at": "", "url": ""},
            {"id": "2", "title": "SWE", "department": "Engineering", "location": "SF", "updated_at": "", "url": ""}]
BOARD_T1 = BOARD_T0 + [{"id": str(i), "title": f"Gov {i}", "department": "Federal", "location": "DC", "updated_at": "", "url": ""} for i in (3, 4, 5)] \
    + [{"id": "6", "title": "SDR", "department": "Sales", "location": "SF", "updated_at": "", "url": ""}]


class Signals(unittest.TestCase):
    def setUp(self):
        self.st = _Store()
        self.p = [mock.patch.object(selfserve, "read_data", side_effect=self.st.read), mock.patch.object(selfserve, "write_data", side_effect=self.st.write),
                  mock.patch.object(selfserve, "update_data", side_effect=self.st.update), mock.patch.object(selfserve, "list_data", side_effect=self.st.list),
                  mock.patch.object(config, "SIGNAL_NEW_DEPT_MIN_ROLES", 3), mock.patch.object(config, "SIGNAL_NEW_DEPT_LOOKBACK_DAYS", 90),
                  mock.patch.object(config, "SIGNAL_MAX_DISPATCHES_PER_DAY", 2), mock.patch.object(config, "SIGNAL_MIN_HOURS_SINCE_CHECK", 6),
                  mock.patch.object(config, "SELFSERVE_GH_TOKEN", "t"), mock.patch.object(config, "SELFSERVE_DISPATCH_REPO", "o/r"), mock.patch.object(config, "SELFSERVE_BRANCH", "main"),
                  mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "")]     # production store -> dispatches main
        for p in self.p:
            p.start()
        self.meta = {"competitor": "Salesforce", "my_company": "HubSpot", "watch": {"edgar_cik": "1108524", "job_boards": [{"host": "ashby", "token": "acme"}]}, "last_checked": "2026-09-28T04:00:00"}
        # outside the anchor band and with no run in progress unless a test says otherwise
        self.p += [mock.patch.object(signals, "_near_anchor", return_value=False), mock.patch.object(signals, "_monitor_busy", return_value=False),
                   mock.patch.object(signals.edgar, "company_name", return_value="Salesforce, Inc.")]
        for p in self.p[-3:]:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_first_poll_only_takes_the_watermark_then_a_filing_dispatches_once(self):
        posted = []
        def post(url, headers=None, timeout=None, json=None):
            posted.append((url, json)); return mock.Mock(status_code=204)
        with mock.patch.object(signals.edgar, "filings", return_value=FILINGS_T0), mock.patch.object(signals.jobs, "postings", return_value=BOARD_T0), \
             mock.patch.object(signals.httpx, "post", side_effect=post):
            r0 = signals.poll_card("hubspot__vs__salesforce", self.meta, today="2026-09-28")
        self.assertEqual((r0["filings"], r0["new_departments"], r0["dispatched"]), (0, 0, None))     # watermark + snapshot only
        self.assertEqual(signals.open_signals("hubspot__vs__salesforce"), [])
        with mock.patch.object(signals.edgar, "filings", return_value=FILINGS_T1), mock.patch.object(signals.jobs, "postings", return_value=BOARD_T1), \
             mock.patch.object(signals.httpx, "post", side_effect=post):
            r1 = signals.poll_card("hubspot__vs__salesforce", self.meta, today="2026-09-29")
            r2 = signals.poll_card("hubspot__vs__salesforce", self.meta, today="2026-09-29")          # same hour again: nothing new
        self.assertEqual((r1["filings"], r1["new_departments"], r1["dispatched"]), (1, 1, "dispatched"))
        self.assertEqual(posted[0][1]["inputs"]["slugs"], "hubspot__vs__salesforce"); self.assertTrue(posted[0][1]["inputs"]["force"])
        self.assertIn("monitor.yml", posted[0][0]); self.assertEqual(posted[0][1]["ref"], "main")
        self.assertEqual((r2["filings"], r2["new_departments"]), (0, 0)); self.assertEqual(len(posted), 1)   # dedupe: no second dispatch
        opened = signals.open_signals("hubspot__vs__salesforce")
        self.assertEqual([s["kind"] for s in opened], ["filing", "new_department"])
        self.assertTrue(opened[0]["trigger"]); self.assertFalse(opened[1]["trigger"])                 # a department never dispatches
        self.assertTrue(opened[0].get("dispatched_at")); self.assertNotIn("dispatched_at", opened[1])   # the filing carries its stamp
        self.assertEqual(opened[1]["department"], "Federal"); self.assertEqual(opened[1]["roles"], 3)
        self.assertEqual((r1["context"][0]["net"], r1["context"][0]["by_department"]["Federal"]), (4, 3))   # the delta the run saw
        self.assertEqual(r2["context"][0]["net"], 0)                                                          # nothing changed since
        ctx = signals.context_block("hubspot__vs__salesforce")
        self.assertIn("HIRING CONTEXT", ctx); self.assertIn("6 open roles", ctx); self.assertIn("context for judgment", ctx)
        # consumed by a run: gone from open, still in the events log
        self.assertEqual(signals.consume("hubspot__vs__salesforce", "2026-09-29T11:00:00"), 2)
        self.assertEqual(signals.open_signals("hubspot__vs__salesforce"), []); self.assertEqual(len(signals.events("hubspot__vs__salesforce")), 2)
        with mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "rc"), mock.patch.object(signals.httpx, "post", side_effect=post):
            self.st.files["signals/_dispatch.json"] = json.dumps({"day": signals._today(), "count": 0, "last": {}})
            signals.dispatch("hubspot__vs__salesforce", "x", self.meta)
        self.assertEqual(posted[-1][1]["ref"], "rc")                                                       # an RC store dispatches rc

    def test_small_hiring_deltas_and_old_departments_are_context_only(self):
        state = {"edgar": {"1108524": {"seen": [], "since": "2026-09-01"}},
                 "boards": {"ashby:acme": {"ids": {"1": "Sales", "2": "Engineering"}, "departments": {"Sales": "2026-01-01", "Engineering": "2026-01-01", "Federal": "2026-08-01"}, "snapshot": {"day": "2026-09-28"}}}}
        with mock.patch.object(signals.jobs, "postings", return_value=BOARD_T1):
            ctx, sig = signals.poll_jobs("ashby", "acme", state, today="2026-09-29")
        self.assertEqual(sig, [])                                                   # Federal was seen 59 days ago: not new
        self.assertEqual((ctx["net"], ctx["added"], ctx["by_department"]["Federal"]), (4, 4, 3))
        state["boards"]["ashby:acme"]["departments"]["Federal"] = "2026-05-01"        # 151 days ago: new again
        with mock.patch.object(signals.jobs, "postings", return_value=BOARD_T1):
            _, sig = signals.poll_jobs("ashby", "acme", state, today="2026-09-29")
        self.assertEqual([s["department"] for s in sig], ["Federal"])
        two = [r for r in BOARD_T1 if r["id"] != "5"]                                # 2 roles: under the minimum
        state["boards"]["ashby:acme"]["departments"]["Federal"] = "2026-05-01"
        with mock.patch.object(signals.jobs, "postings", return_value=two):
            _, sig = signals.poll_jobs("ashby", "acme", state, today="2026-09-29")
        self.assertEqual(sig, [])

    def test_dispatch_caps(self):
        self.st.files["signals/_dispatch.json"] = json.dumps({"day": signals._utcnow().date().isoformat(), "count": 2, "last": {}})   # the cap counts UTC days (local today != UTC today after 17:00 PT)
        self.assertEqual(signals.may_dispatch("a", self.meta)[0], False)
        self.st.files["signals/_dispatch.json"] = json.dumps({"day": signals._utcnow().date().isoformat(), "count": 0, "last": {}})
        recent = dict(self.meta, last_checked=signals._utcnow().isoformat(timespec="seconds"))
        ok, why = signals.may_dispatch("a", recent); self.assertFalse(ok); self.assertIn("within", why)
        self.assertTrue(signals.may_dispatch("a", self.meta)[0])
        with mock.patch.object(config, "SELFSERVE_GH_TOKEN", ""):
            self.assertEqual(signals.dispatch("a", "x", self.meta), (False, "no dispatch token"))
        with mock.patch.object(signals.httpx, "post", return_value=mock.Mock(status_code=403)):
            self.assertEqual(signals.dispatch("a", "x", self.meta)[0], False)
        self.assertEqual(json.loads(self.st.files["signals/_dispatch.json"])["count"], 0)          # a failed dispatch is not counted

    def test_a_refused_dispatch_is_retried_next_hour_from_the_store(self):
        with mock.patch.object(signals.edgar, "filings", return_value=FILINGS_T0), mock.patch.object(signals.jobs, "postings", return_value=BOARD_T0):
            signals.poll_card("s", self.meta, today="2026-09-28")
        with mock.patch.object(signals.edgar, "filings", return_value=FILINGS_T1), mock.patch.object(signals.jobs, "postings", return_value=BOARD_T0),              mock.patch.object(signals, "_monitor_busy", return_value=True):                              # hour 1: a run is in progress
            r1 = signals.poll_card("s", self.meta, today="2026-09-29")
        self.assertEqual(r1["filings"], 1); self.assertIn("in progress", r1["dispatched"])
        self.assertFalse(signals.open_signals("s")[0].get("dispatched_at"))
        posted = []
        with mock.patch.object(signals.edgar, "filings", return_value=FILINGS_T1), mock.patch.object(signals.jobs, "postings", return_value=BOARD_T0),              mock.patch.object(signals.httpx, "post", side_effect=lambda url, **kw: posted.append(kw) or mock.Mock(status_code=204)):
            r2 = signals.poll_card("s", self.meta, today="2026-09-29")                                   # hour 2: nothing new, still dispatches
        self.assertEqual((r2["filings"], r2["dispatched"]), (0, "dispatched")); self.assertEqual(len(posted), 1)
        self.assertTrue(signals.open_signals("s")[0].get("dispatched_at"))
        with mock.patch.object(signals, "_near_anchor", return_value=True):
            ok, why = signals.may_dispatch("s", self.meta); self.assertFalse(ok); self.assertIn("scheduled run", why)

    def test_clocks_are_utc(self):
        # the mini runs on Pacific; last_checked is the runner's naive UTC. A check 5 h ago in UTC is "within 6 h"
        five_h_ago_utc = (signals._utcnow() - signals.timedelta(hours=5)).isoformat(timespec="seconds")
        ok, why = signals.may_dispatch("s", dict(self.meta, last_checked=five_h_ago_utc)); self.assertFalse(ok); self.assertIn("within", why)
        seven_h_ago_utc = (signals._utcnow() - signals.timedelta(hours=7)).isoformat(timespec="seconds")
        self.assertTrue(signals.may_dispatch("s", dict(self.meta, last_checked=seven_h_ago_utc))[0])
        self.assertRegex(signals._now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_stale_open_signals_drop_out(self):
        old = {"kind": "filing", "fingerprint": "old", "detected_at": "2026-01-01T00:00:00", "trigger": True}
        signals.append_events("s", [old, dict(old, fingerprint="new", detected_at=signals._now())])
        self.assertEqual([e["fingerprint"] for e in signals.open_signals("s")], ["new"])

    def test_watch_and_missing_store_are_safe(self):
        self.assertEqual(signals.watch_for({}), {"edgar_ciks": [], "job_boards": []})
        self.assertEqual(signals.watch_for({"watch": {"edgar_cik": "1", "edgar_ciks": ["2", "1"]}})["edgar_ciks"], ["1", "2"])
        self.assertEqual(signals.poll_card("x", {"competitor": "X"})["skipped"], "no watch")
        self.assertEqual(signals.open_signals("nothing"), []); self.assertEqual(signals.context_block("nothing"), "")
