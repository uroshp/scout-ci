"""scripts/replay_calls.py + scripts/model_checkin.py: the runner's spend-safety and idempotency
(per call/backend/mode/rep, month-sharded; a rep needs its rep-0 twin), backend ordering, window /
deadline exits, the 14-day lookback replacing shared state, and the check-in's rule that the streak
advances only with --snapshot. Store, backends and the loop are mocked; nothing is called."""
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from scout import modelcompare as mc, replaybackends as rb, selfserve

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rc = _load("replay_calls")
mck = _load("model_checkin")

JUDGE_REF = '{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"r"}]}'


def call(call_id, role="judge", run_ts="2026-09-27T04:10:00", slug="a__vs__b"):
    from scout import rolespecs
    return {"call_id": call_id, "role": role, "slug": slug, "run_ts": run_ts, "source": "monitor", "status": "ok",
            "fidelity": "exact" if role == "judge" else "toolsful",
            "model": rolespecs.spec(role)["primary_model"](),
            "system": {"kind": "string", "text": "S"} if role == "judge" else {"kind": "preset", "preset": "claude_code", "append": "A"},
            "user": "U", "transcript": [], "result": {"text": JUDGE_REF if role == "judge" else '{"has_candidates":false,"candidates":[]}',
                                                       "cost_usd": 0.2}, "sizes": {"est_tokens_in": 1000}, "truncated": [],
            "options": {"max_turns": 4}}


class _Store:
    """In-memory private store: calls/ bundles in, shadow_models/ results out."""

    def __init__(self, calls):
        self.files = {"calls/2026-09/monitor_20260927T041000_abc123.json": json.dumps({"calls": calls})}
        self.writes = []

    def list_data(self, path, include_dirs=False):
        if path == "costs":
            return ["ledger.json"]
        out = set()
        for p in self.files:
            if p.startswith(path + "/"):
                rest = p[len(path) + 1:]
                head = rest.split("/")[0]
                if "/" in rest and not include_dirs:
                    continue
                out.add(head)
        return sorted(out)

    def read_data(self, path):
        return self.files.get(path)

    def write_data(self, path, text, message):
        self.files[path] = text
        self.writes.append(path)


def _run(argv, store, *, replay=None, loop=None, now=None):
    ok = {"status": "ok", "reason": None, "text": JUDGE_REF, "duration_ms": 5, "backend_model": "m",
          "backend_version": {"v": 1}, "cost_usd": 0.0}
    patches = [
        mock.patch.object(sys, "argv", ["replay_calls.py"] + argv),
        mock.patch.object(selfserve, "use_github", return_value=True),
        mock.patch.object(selfserve, "list_data", side_effect=store.list_data),
        mock.patch.object(selfserve, "read_data", side_effect=store.read_data),
        mock.patch.object(selfserve, "write_data", side_effect=store.write_data),
        mock.patch.object(rb, "drive_replay", side_effect=replay or (lambda c, b, **kw: dict(ok))),
        mock.patch.object(rb, "ollama_resident", return_value=None),
        mock.patch.object(rb, "ollama_unload"),
        mock.patch.object(rc, "_save_state"), mock.patch.object(rc, "_state", return_value={}),
        mock.patch.object(rc, "_load_env"),
    ]
    if loop is not None:
        import scout.localagent as la
        patches.append(mock.patch.object(la, "run_loop", side_effect=loop))
        patches.append(mock.patch.object(rc.httpx, "get", return_value=mock.Mock(status_code=200)))
    if now is not None:
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        patches.append(mock.patch.object(rc, "datetime", _DT))
    buf = io.StringIO()
    with redirect_stdout(buf):
        for p in patches:
            p.start()
        try:
            rc.main()
        finally:
            for p in patches:
                p.stop()
    return buf.getvalue()


class Runner(unittest.TestCase):
    def test_default_is_estimate_only_and_calls_nothing(self):
        store = _Store([call("c_1"), call("c_2")])
        with mock.patch.object(rb, "drive_replay") as dr:
            out = _run(["--backend", "apple_ondevice", "--no-window"], store)
        self.assertIn("ESTIMATE ONLY", out); self.assertIn("planned=2", out)
        self.assertEqual(store.writes, [])

    def test_run_without_write_persists_nothing(self):
        store = _Store([call("c_1")])
        out = _run(["--backend", "apple_ondevice", "--no-window", "--run"], store)
        self.assertIn("NOT persisted", out); self.assertEqual(store.writes, [])

    def test_write_lands_month_sharded_and_is_idempotent(self):
        store = _Store([call("c_1"), call("c_2", run_ts="2026-10-01T04:00:00")])
        _run(["--backend", "apple_ondevice", "--no-window", "--run", "--write"], store)
        self.assertEqual(sorted(store.writes), ["shadow_models/apple_ondevice/2026-09/c_1.json",
                                                "shadow_models/apple_ondevice/2026-10/c_2.json"])
        store.writes.clear()
        out = _run(["--backend", "apple_ondevice", "--no-window", "--run", "--write"], store)
        self.assertEqual(store.writes, []); self.assertIn("0 replayed, 2 already done", out)
        # --force replays anyway (overwrites, same paths)
        _run(["--backend", "apple_ondevice", "--no-window", "--run", "--write", "--force"], store)
        self.assertEqual(len(store.writes), 2)

    def test_backends_run_in_the_given_order_each_with_its_own_done_set(self):
        store = _Store([call("c_1")])
        seen = []
        def replay(c, b, **kw):
            seen.append(b)
            return {"status": "ok", "reason": None, "text": JUDGE_REF, "duration_ms": 1, "backend_model": b, "backend_version": {}, "cost_usd": 0.0}
        _run(["--backend", "ollama", "--backend", "apple_ondevice", "--no-window", "--run", "--write"], store, replay=replay)
        self.assertEqual(seen, ["ollama", "apple_ondevice"])
        self.assertEqual(sorted(store.writes), ["shadow_models/apple_ondevice/2026-09/c_1.json", "shadow_models/ollama/2026-09/c_1.json"])

    def test_anthropic_refused_without_allow_spend(self):
        store = _Store([call("c_1")])
        with mock.patch.object(rb, "drive_replay") as dr:
            out = _run(["--backend", "anthropic", "--no-window", "--run", "--write"], store)
        self.assertIn("refused", out); dr.assert_not_called(); self.assertEqual(store.writes, [])

    def test_modes_pick_the_right_roles_and_rep_needs_its_twin(self):
        store = _Store([call("c_j"), call("c_t", role="triage")])
        out = _run(["--backend", "ollama", "--no-window"], store)
        self.assertIn("exact  judge", out); self.assertNotIn("triage", out.split("ESTIMATE")[0].split("planned")[1])
        out = _run(["--backend", "ollama", "--no-window", "--mode", "all"], store)
        self.assertIn("frozen triage", out); self.assertIn("loop   triage", out); self.assertIn("planned=3", out)
        loop_ok = {"status": "ok", "reason": None, "text": '{"has_candidates":false,"candidates":[]}', "duration_ms": 1,
                   "backend_model": "m", "backend_version": {}, "cost_usd": 0.0, "loop": {"tool_protocol": "native", "tool_calls": [], "hosts": [], "tool_failure": False}}
        # a rep-1 loop run with no rep-0 result is skipped
        _run(["--backend", "ollama", "--no-window", "--mode", "loop", "--repeat", "1", "--run", "--write"], store, loop=lambda c, b, rep=0: dict(loop_ok))
        self.assertEqual(store.writes, [])
        _run(["--backend", "ollama", "--no-window", "--mode", "loop", "--run", "--write"], store, loop=lambda c, b, rep=0: dict(loop_ok))
        self.assertEqual(store.writes, ["shadow_models/ollama/2026-09/c_t.loop.json"])
        _run(["--backend", "ollama", "--no-window", "--mode", "loop", "--repeat", "1", "--run", "--write"], store, loop=lambda c, b, rep=0: dict(loop_ok))
        self.assertEqual(store.writes[-1], "shadow_models/ollama/2026-09/c_t.loop.r1.json")

    def test_window_and_deadline_stop_cleanly(self):
        store = _Store([call("c_1"), call("c_2")])
        out = _run(["--backend", "apple_ondevice", "--run", "--write", "--window", "01:00-06:00"], store,
                   now=datetime(2026, 9, 29, 12, 0, 0))
        self.assertIn("outside the window", out); self.assertEqual(store.writes, [])
        out = _run(["--backend", "apple_ondevice", "--run", "--write", "--no-window", "--deadline", "03:00"], store,
                   now=datetime(2026, 9, 29, 3, 0, 0))
        self.assertIn("deadline reached", out); self.assertEqual(store.writes, [])

    def test_lookback_default_replaces_shared_state(self):
        old = call("c_old", run_ts="2026-08-01T04:00:00")
        store = _Store([old])
        store.files["calls/2026-08/monitor_20260801T040000_old001.json"] = store.files.pop("calls/2026-09/monitor_20260927T041000_abc123.json")
        out = _run(["--backend", "apple_ondevice", "--no-window"], store, now=datetime(2026, 9, 29, 3, 0, 0))
        self.assertIn("bundles=0", out)
        out = _run(["--backend", "apple_ondevice", "--no-window", "--since", "20260701T000000"], store, now=datetime(2026, 9, 29, 3, 0, 0))
        self.assertIn("bundles=1", out)

    def test_creds_missing_hard_exits_before_any_store_call(self):
        with mock.patch.object(selfserve, "use_github", return_value=False), mock.patch.object(selfserve, "list_data") as ld:
            with self.assertRaises(SystemExit) as cm:
                rc._require_store()
        self.assertIn("CREDS MISSING", str(cm.exception)); ld.assert_not_called()
        with mock.patch.object(selfserve, "use_github", return_value=True), mock.patch.object(selfserve, "list_data", return_value=[]):
            with self.assertRaises(SystemExit) as cm:
                rc._require_store()
        self.assertIn("STORE READ BROKEN", str(cm.exception))


class Checkin(unittest.TestCase):
    def _results(self, n_adj_right, n_adj_wrong, backend="apple_ondevice"):
        """n disagreeing judge items with labels; enough distinct calls/cards for sufficiency."""
        from tests.test_modelcompare import rec, rep
        rows, labels = [], {}
        cand = '{"verdicts":[{"op_index":0,"verdict":"reject","reason":"r"}]}'
        for i in range(n_adj_right + n_adj_wrong):
            r = rec("judge", JUDGE_REF, call_id=f"c_{i:03d}", slug=f"card{i % 3}")
            rp = rep(cand, backend=backend)
            row = mc.result_record(r, rp, mc.compare_call(r, rp), backend=backend, mode="exact")
            rows.append(row)
            did = row["comparison"]["items"][0]["delta_id"]
            labels[did] = {"truth": "reject" if i < n_adj_right else "confirm"}
        return rows, labels

    def test_streak_advances_only_with_snapshot(self):
        rows, labels = self._results(6, 12)                      # precision 0.33 < bar, 18 adjudicated
        prior_snap = {"cells": {"apple_ondevice|judge": {"period_key": json.dumps(sorted({'{"v": 1}|' + rows[0]["reference"]["model"] + "+instr:" + rows[0]["instructions_sha"]})),
                                                         "precision": 0.4, "adjudicated": 18, "no_improve_streak": 1}}}
        store = {"model_checkin/20260915T060000.json": json.dumps(prior_snap)}
        with mock.patch.object(mc, "load_results", return_value=rows), mock.patch.object(mc, "load_labels", return_value=labels), \
             mock.patch.object(selfserve, "list_data", side_effect=lambda p, include_dirs=False: [k.split("/")[-1] for k in store if k.startswith(p + "/")]), \
             mock.patch.object(selfserve, "read_data", side_effect=store.get), \
             mock.patch.object(mck, "_recent_bundles", return_value=[]):
            snap1, body1 = mck.build(datetime(2026, 9, 29, 1, 30))
            snap2, body2 = mck.build(datetime(2026, 9, 30, 1, 30))      # a second night, nothing snapshotted
        c1, c2 = snap1["cells"]["apple_ondevice|judge"], snap2["cells"]["apple_ondevice|judge"]
        self.assertEqual(c1["no_improve_streak"], 2)                      # prior streak 1 + this below-bar, not improving
        self.assertEqual(c2["no_improve_streak"], 2)                      # unchanged: the prior did not advance
        self.assertEqual(c1["verdict"], "DIAGNOSE")
        self.assertIn("| apple_ondevice | judge | full |", body1)

    def test_new_period_resets_the_prior(self):
        rows, labels = self._results(10, 8)
        prior_snap = {"cells": {"apple_ondevice|judge": {"period_key": '["OLD"]', "precision": 0.2, "adjudicated": 18, "no_improve_streak": 2}}}
        store = {"model_checkin/20260915T060000.json": json.dumps(prior_snap)}
        with mock.patch.object(mc, "load_results", return_value=rows), mock.patch.object(mc, "load_labels", return_value=labels), \
             mock.patch.object(selfserve, "list_data", side_effect=lambda p, include_dirs=False: [k.split("/")[-1] for k in store if k.startswith(p + "/")]), \
             mock.patch.object(selfserve, "read_data", side_effect=store.get), \
             mock.patch.object(mck, "_recent_bundles", return_value=[]):
            snap, body = mck.build(datetime(2026, 9, 29, 1, 30))
        self.assertIn("new period, no prior", body)
        self.assertEqual(snap["cells"]["apple_ondevice|judge"]["no_improve_streak"], 0)
        self.assertEqual(snap["cells"]["apple_ondevice|judge"]["verdict"], "BASELINE")


if __name__ == "__main__":
    unittest.main()
