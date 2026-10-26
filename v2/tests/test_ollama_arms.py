"""Three Ollama arms, one resident at a time, each its own name/results/column; the labelling
surface stays blind; a silent arm is a canary finding (2026-09-30)."""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scout import modelcompare as mc, replaybackends as rb, localagent

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, f"{name}.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class _Resp:
    def __init__(self, status, body): self.status_code, self._b, self.text = status, body, json.dumps(body)
    def json(self): return self._b


def _rec(role="gate_judge"):
    return {"call_id": "c_arm", "role": role, "run_ts": "2026-09-30T04:00:00", "model": "claude-opus-5",
            "system": {"kind": "string", "text": "sys"}, "user": "usr", "sizes": {"est_tokens_in": 10}, "truncated": [],
            "result": {"text": '{"verdict":"confirm","reason":"r"}', "cost_usd": 0.1}, "options": {}}


class Registry(unittest.TestCase):
    def test_three_arms_registered_and_unknown_still_raises(self):
        for name in ("ollama", "ollama_nemotron", "ollama_gemma4"):
            self.assertIn(name, rb.BACKENDS); self.assertIn(name, rb.LOCAL_BACKENDS)
            self.assertTrue(rb.is_ollama(name)); self.assertIn("tag", rb.ollama_cfg(name))
            self.assertIn(name, localagent.TOOL_PROTOCOL)
        self.assertFalse(rb.is_ollama("apple_ondevice"))
        for bad in ("ollama.gemma", "ollama_lightning", "mistral_hosted"):
            with self.assertRaises(rb.UnknownBackend):
                rb.drive_replay(_rec(), bad)
        self.assertTrue(all("." not in n for n in rb.OLLAMA_MODELS))   # load_results skips dotted folders
        self.assertEqual(rb.OLLAMA_MODELS["ollama"]["tag"], rb.OLLAMA_TAG)   # Magistral keeps its name

    def test_each_arm_sends_its_own_tag_and_context(self):
        body = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}'},
                "prompt_eval_count": 8, "eval_count": 3}
        for name in ("ollama_nemotron", "ollama_gemma4"):
            with mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)) as post, \
                 mock.patch.object(rb, "ollama_version", return_value={"tag": rb.ollama_cfg(name)["tag"]}):
                r = rb.drive_replay(_rec(), name)
            sent = post.call_args.kwargs["json"]
            self.assertEqual(sent["model"], rb.ollama_cfg(name)["tag"])
            self.assertEqual(sent["options"]["num_ctx"], rb.ollama_cfg(name)["num_ctx"])
            self.assertEqual(sent["options"]["num_predict"], rb.rolespecs.output_reserve("gate_judge") + rb.ollama_cfg(name)["think_reserve"])   # bounded generation
            self.assertEqual(r["status"], "ok"); self.assertEqual(r["backend_model"], rb.ollama_cfg(name)["tag"])
            self.assertIsNone(r["thinking"])       # a model with no `thinking` field still parses

    def test_fit_uses_the_arms_own_window(self):
        with mock.patch.dict(rb.OLLAMA_MODELS["ollama_nemotron"], {"num_ctx": 64}):
            ok, reason, _ = rb.fits(_rec(), "ollama_nemotron", "x" * 3000, "y" * 3000)
        self.assertEqual((ok, reason), (False, "context_exceeded"))
        ok, reason, _ = rb.fits(_rec(), "ollama_gemma4", "x" * 3000, "y" * 3000)
        self.assertTrue(ok)

    def test_resident_and_unload_target_the_arms_tag(self):
        with mock.patch.object(rb.httpx, "post") as post:
            rb.ollama_unload("ollama_gemma4")
        self.assertEqual(post.call_args.kwargs["json"]["model"], rb.ollama_cfg("ollama_gemma4")["tag"])


class Scorecard(unittest.TestCase):
    def _row(self, backend, call_id, role="gate_judge"):
        return {"backend": backend, "call_id": call_id, "role": role, "mode": "exact", "rep": 0, "status": "ok",
                "reason": None, "comparison": {"summary": {"agree": 1, "judged": 1}}, "duration_ms": 10,
                "reference": {"eligible": True}}

    def test_warming_or_lagging_arm_never_shrinks_common(self):
        rows = [self._row("ollama", f"c{i}") for i in range(25)] + [self._row("apple_ondevice", f"c{i}") for i in range(25)]
        rows += [self._row("ollama_gemma4", "c1")]        # one result: warming up
        sc = mc.scorecard(rows, labels={})
        self.assertEqual(sc["common_n"], 25); self.assertEqual(sc["warming_up"], ["ollama_gemma4"])
        rows += [self._row("ollama_gemma4", f"c{i}") for i in range(2, 22)]   # 21 of 25 = 84%: no longer warming, still not comparable
        sc = mc.scorecard(rows, labels={})
        self.assertEqual(sc["warming_up"], []); self.assertEqual(sc["common_n"], 25)
        self.assertFalse(sc["cells"]["ollama_gemma4|gate_judge"]["comparable"])
        rows += [self._row("ollama_gemma4", f"c{i}") for i in (22, 23)]      # 23 of 25 = 92%: joins, and the population is its 23
        sc = mc.scorecard(rows, labels={})
        self.assertTrue(sc["cells"]["ollama_gemma4|gate_judge"]["comparable"]); self.assertEqual(sc["common_n"], 23)


class Blind(unittest.TestCase):
    def test_digest_never_names_an_arm(self):
        from scout import adjudicate_models as am
        pend = [{"delta_id": "m_1", "role": "gate_judge", "slug": "s", "item_id": "i",
                 "candidates": ["ollama_gemma4", "ollama_nemotron"]}]
        buf = io.StringIO()
        with mock.patch.object(am, "pending", return_value=pend), mock.patch.object(mc, "load_labels", return_value={}), \
             redirect_stdout(buf):
            am._print_digest()
        out = buf.getvalue()
        for name in rb.OLLAMA_MODELS:
            self.assertNotIn(name, out)
        self.assertIn("disagreed by 2 arms", out)


class Freshness(unittest.TestCase):
    def test_capture_staleness_follows_the_monitor_calendar(self):
        """2026-10-25: the monitor skips Sunday, so the Sunday 23:00 replay found Saturday's capture
        and seven invocations each emailed "no fresh capture". The rule is the monitor's calendar,
        not an hour count, and the script only logs; the lanes canary reports a missing capture."""
        rc_ = _script("replay_calls")
        from datetime import datetime
        sat = "20261024T110142"                                   # Saturday 10/24, 04:01 PDT
        self.assertIsNone(rc_.stale_capture(sat, datetime(2026, 10, 25, 23, 0)))          # Sunday night
        why = rc_.stale_capture(sat, datetime(2026, 10, 26, 23, 0))                          # Monday night
        self.assertIn("Sat 10/24", why); self.assertIn("Mon 10/26", why)
        self.assertIsNone(rc_.stale_capture("20261022T110053", datetime(2026, 10, 23, 0, 22)))   # 00:22 catch-up
        self.assertIsNone(rc_.stale_capture("20261026T110000", datetime(2026, 10, 26, 23, 0)))
        self.assertEqual(rc_.stale_capture(None, datetime(2026, 10, 26, 23, 0)), "no capture bundle at all")
        self.assertIn("unreadable", rc_.stale_capture("garbage", datetime(2026, 10, 26, 23, 0)))
        src = open(os.path.join(_SCRIPTS, "replay_calls.py")).read()
        self.assertNotIn("send_could_not_run", src)


class Canary(unittest.TestCase):
    def test_silent_arm_is_a_finding(self):
        lc = _script("lanes_canary")
        log = "2026-10-01 05:15:00 PDT  === replay start\n2026-10-01 05:18:00 PDT  apple arm exit 0\n" \
              "2026-10-01 05:40:00 PDT  ollama arm exit 0\n2026-10-01 06:10:00 PDT  ollama_nemotron arm exit 0\n"
        probs = lc.arm_problems(log)
        self.assertEqual(len(probs), 1); self.assertIn("ollama_gemma4", probs[0])
        self.assertEqual(lc.arm_problems(log + "2026-10-01 06:50:00 PDT  ollama_gemma4 arm exit 0\n"), [])
        self.assertEqual(lc.arm_problems("2026-10-01 05:15:00 PDT  ollama arms skipped tonight\n"), [])
        # an arm the runner skipped on purpose (the run's deadline passed before it started) left its trace
        self.assertEqual(lc.arm_problems(log + "2026-10-24 06:31:00 PDT  ollama_gemma4 arm skipped (deadline 06:30 "
                                               "passed before it started; it catches up tomorrow night)\n"), [])

    def test_arbiter_silence_is_a_finding(self):
        lc = _script("lanes_canary")
        base = "2026-10-19 05:15:00 PDT  === replay start\n2026-10-19 10:56:38 PDT  --- arbiter ---\n"
        end = "2026-10-19 11:03:54 PDT  === replay end ===\n"
        ok = "[arbiter] pending 41, not yet arbitrated 7, this run up to 20\n[arbiter] 7 arbitrated\n2026-10-19 11:03:54 PDT  arbiter exit 0\n"
        self.assertEqual(lc.arbiter_problems(base + ok + end), [])
        self.assertEqual(lc.arbiter_problems(base + "[arbiter] pending 41, not yet arbitrated 0, this run up to 20\n[arbiter] 0 arbitrated\narbiter exit 0\n" + end), [])
        dead = lc.arbiter_problems(base + "Traceback (most recent call last):\n  File x\nhttpx.HTTPStatusError: Client error '403 Forbidden' for url 'u'\n"
                                   "2026-10-19 11:03:54 PDT  arbiter exit 1\n" + end)
        self.assertEqual(len(dead), 1); self.assertIn("did not rule today", dead[0]); self.assertIn("403", dead[0])
        self.assertEqual(lc.arbiter_problems(base + "[arbiter] 7 arbitrated\narbiter exit 1\n" + end), ["arbiter: exit 1 today after ruling on 7"])
        stuck = lc.arbiter_problems(base + "[arbiter] pending 41, not yet arbitrated 7, this run up to 20\n[arbiter] 0 arbitrated\narbiter exit 0\n" + end)
        self.assertEqual(len(stuck), 1); self.assertIn("0 of 7", stuck[0])
        capped = base + "[arbiter] pending 41, not yet arbitrated 7, this run up to 20\n[arbiter] daily ceiling reached ($5.00); stopping\n[arbiter] 0 arbitrated\n" + end
        self.assertEqual(lc.arbiter_problems(capped), [])
        self.assertEqual(lc.arbiter_problems("2026-10-19 05:15:00 PDT  === replay start\n"), [])     # still running: not a finding
        self.assertEqual(len(lc.arbiter_problems("2026-10-19 05:15:00 PDT  === replay start\n" + end)), 1)   # ended without the section

    def test_night_lane_run_is_judged_by_its_start_marker(self):
        import json as _json, os as _os, tempfile
        from datetime import date as _date, datetime as _dt
        lc = _script("lanes_canary")
        run = ("2026-10-23 23:00:05 PDT  === replay start (deadline 06:30, arm cap 240 min) ===\n"
               "bundles=22 since=20261009T230005  calls=269  planned=403  backends=['apple_ondevice']\n"
               "newest bundle: 20261023T110053\n"
               "2026-10-23 23:07:00 PDT  apple arm exit 0 (attempt 1)\n"
               "2026-10-24 01:30:00 PDT  ollama arm exit 0 (attempt 1)\n"
               "2026-10-24 02:00:00 PDT  ollama_nemotron arm exit 0 (attempt 1)\n"
               "2026-10-24 02:25:00 PDT  ollama_gemma4 arm exit 0 (attempt 1)\n"
               "2026-10-24 02:26:00 PDT  --- arbiter ---\n[store] mirror abc (pulled; bulk reads from disk)\n"
               "[arbiter] pending 41, not yet arbitrated 0, this run up to 20\n[arbiter] 0 arbitrated\n"
               "2026-10-24 02:26:10 PDT  arbiter exit 0\n2026-10-24 02:26:10 PDT  === replay end ===\n")
        old = ("2026-10-22 05:15:05 PDT  === replay start (window 05:00-15:00, deadline 15:00) ===\n"
               "newest bundle: 20261022T110053\n2026-10-22 10:50:10 PDT  === replay end ===\n")
        self.assertTrue(lc.latest_replay_run(old + run).startswith("2026-10-23 23:00:05"))
        self.assertEqual(lc.latest_replay_run("no runs here"), "")
        with tempfile.TemporaryDirectory() as d:
            def write(text):
                open(_os.path.join(d, "launchd.out.log"), "w").write(text)
            _json.dump({"last_run": "2026-10-24T02:25:00"}, open(_os.path.join(d, "state.json"), "w"))
            write(old + run)
            with mock.patch.object(lc, "REPLAY_DIR", d), mock.patch.object(lc, "_replay_running", return_value=False):
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 9, 0)):     # the morning after: clean
                    self.assertEqual(lc.check_replay(_date(2026, 10, 24)), [])
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 15, 15)):   # the afternoon pass: still clean
                    self.assertEqual(lc.check_replay(_date(2026, 10, 24)), [])
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 25, 9, 0)):     # the next night did not run
                    probs = lc.check_replay(_date(2026, 10, 25))
                    self.assertEqual(len(probs), 1); self.assertIn("did not run", probs[0])
                # a catch-up started after midnight scores the morning before: fresh, not a backlog
                write(old + run.replace("2026-10-23 23:00:05", "2026-10-24 00:22:43"))
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 9, 0)):
                    self.assertEqual(lc.check_replay(_date(2026, 10, 24)), [])
                # a run that scored only old bundles is still a finding
                write(old + run.replace("newest bundle: 20261023T110053", "newest bundle: 20261020T110053"))
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 9, 0)):
                    probs = lc.check_replay(_date(2026, 10, 24))
                    self.assertEqual(len(probs), 1); self.assertIn("scored a backlog", probs[0])
            # in progress is not a finding; alive 12 h after it began is
            write(old + run)
            with mock.patch.object(lc, "REPLAY_DIR", d), mock.patch.object(lc, "_replay_running", return_value=True):
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 9, 0)):
                    self.assertEqual(lc.check_replay(_date(2026, 10, 24)), [])
                with mock.patch.object(lc, "_now", return_value=_dt(2026, 10, 24, 12, 0)):
                    probs = lc.check_replay(_date(2026, 10, 24))
                    self.assertEqual(len(probs), 1); self.assertIn("still running", probs[0])

    def test_mirror_pull_failure_is_a_finding(self):
        lc = _script("lanes_canary")
        base = "2026-10-20 05:15:00 PDT  === replay start\n2026-10-20 09:40:00 PDT  --- read-out ---\n"
        end = "2026-10-20 10:03:54 PDT  === replay end ===\n"
        self.assertEqual(lc.mirror_problems(base + "[store] mirror a611f49e (pulled; bulk reads from disk)\n" + end), [])
        bad = lc.mirror_problems(base + "[store] mirror pull failed (fatal: could not read Username); reading what is on disk (a611f49e)\n" + end)
        self.assertEqual(len(bad), 1); self.assertIn("stale copy", bad[0]); self.assertIn("could not read Username", bad[0])
        self.assertEqual(lc.mirror_problems(base + "[store] mirror pull failed (x)\n"), [])    # still running: not yet a finding
        self.assertEqual(lc.mirror_problems(""), [])


if __name__ == "__main__":
    unittest.main()
