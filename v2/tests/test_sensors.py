"""Sensors (Release 2): the pure modules, the screen's validation, the compare matchers and the
streak, and the monitor's shadow / gate behaviour with every model and network call faked. No spend."""
import json
import unittest
from unittest import mock

from scout import config
from scout.sensors import compare, events, feeds, news, pagediff, registry, screen

PRICING = """<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>
<nav><a href="/">Home</a></nav><main><h1>Plans and pricing for every team size today</h1>
<p>Updated October 3, 2026</p>
<p>The Team plan costs $20 per seat per month and includes unlimited projects and SSO.</p>
<table><tr><th>Plan</th><th>Price</th><th>Seats</th></tr>
<tr><td>Team</td><td>$20 / seat / month</td><td>Up to 250 seats included</td></tr>
<tr><td>Enterprise</td><td>Custom pricing with annual commitment</td><td>Unlimited seats and dedicated support</td></tr></table>
<ul><li>Priority support is included on every paid plan at no extra cost.</li><li>1,204 views</li></ul>
</main><footer>© 2026 Acme Inc. All rights reserved.</footer></body></html>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acme News</title>
<item><title>Acme launches Widget 3 with usage-based pricing</title><link>https://acme.example/news/widget-3</link>
<guid>w3</guid><pubDate>Fri, 03 Oct 2026 14:00:00 GMT</pubDate><description>Widget 3 ships today with a new pricing model.</description></item>
<item><title>Acme hires a new CRO</title><link>https://acme.example/news/cro</link><guid>cro</guid>
<pubDate>Thu, 02 Oct 2026 09:00:00 GMT</pubDate><description>Jane Doe joins from Globex.</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Acme Blog</title>
<entry><id>tag:acme,2026:1</id><title>Why we changed our limits</title><link rel="alternate" href="/blog/limits"/>
<published>2026-10-03T10:00:00Z</published><summary>Our new rate limits explained.</summary></entry></feed>"""

GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>"Acme" - Google News</title>
<item><title>Acme raises $500M at a $5B valuation - TechCrunch</title><link>https://news.google.com/rss/articles/CBMi</link>
<guid>g1</guid><pubDate>Fri, 03 Oct 2026 12:00:00 GMT</pubDate><source url="https://techcrunch.com">TechCrunch</source></item>
<item><title>Acme is hiring again - Reddit</title><link>https://news.google.com/rss/articles/CBMj</link><guid>g2</guid>
<pubDate>Fri, 03 Oct 2026 11:00:00 GMT</pubDate><source url="https://www.reddit.com">Reddit</source></item>
<item><title>Why Acme matters for widget buyers</title><link>https://news.google.com/rss/articles/CBMk</link><guid>g3</guid>
<pubDate>Fri, 03 Oct 2026 10:00:00 GMT</pubDate><source url="https://randomblog.example">Random Blog</source></item>
</channel></rss>"""

BNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>acme - Bing News</title>
<item><title>Acme raises $500M at a $5B valuation</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2ftechcrunch.com%2f2026%2f10%2f03%2facme-raises%2f&amp;c=1&amp;mkt=en-us</link>
<pubDate>Fri, 03 Oct 2026 12:05:00 GMT</pubDate><description>The round values Acme at $5B.</description></item>
</channel></rss>"""


class PageDiff(unittest.TestCase):
    def test_blocks_are_elements_and_rows_and_drop_nav_and_footer(self):
        b = pagediff.blocks_from_html(PRICING)
        self.assertIn("Team | $20 / seat / month | Up to 250 seats included", b)
        self.assertTrue(any(x.startswith("The Team plan costs $20") for x in b))
        self.assertFalse(any("Home" == x for x in b))
        self.assertFalse(any("All rights reserved" in x for x in b))

    def test_noise(self):
        for n in ("Updated October 3, 2026", "1,204 views", "© 2026 Acme Inc. All rights reserved.", "12:40 PM PT", "2026-10-03"):
            self.assertTrue(pagediff.is_noise(n), n)
        self.assertFalse(pagediff.is_noise("The Team plan costs $20 per seat per month and includes unlimited projects."))

    def test_value_change_carries_old_and_new_and_is_not_dropped(self):
        b0 = pagediff.blocks_from_html(PRICING)
        st = pagediff.diff(None, b0)
        self.assertTrue(st["first"])
        b1 = pagediff.blocks_from_html(PRICING.replace("$20", "$25").replace("October 3", "October 4"))
        d = pagediff.diff(st["state"], b1)
        self.assertFalse(d["redesign"])
        self.assertEqual(len(d["value_changes"]), 2)
        self.assertIn("$20", d["value_changes"][0]["old"]); self.assertIn("$25", d["value_changes"][0]["new"])
        self.assertEqual(d["new"], [])                       # the date stamp changed too: noise, not a finding

    def test_redesign_is_flagged_not_dropped(self):
        st = pagediff.diff(None, pagediff.blocks_from_html(PRICING))
        new = "<html><body>" + "".join(f"<p>Completely different paragraph number {i} about a new launch.</p>" for i in range(12)) + "</body></html>"
        d = pagediff.diff(st["state"], pagediff.blocks_from_html(new))
        self.assertTrue(d["redesign"]); self.assertGreater(d["ratio"], 0.6)


class Feeds(unittest.TestCase):
    def test_rss_and_atom(self):
        r = feeds.parse(RSS, "https://acme.example/feed.xml")
        self.assertEqual([i["id"] for i in r], ["w3", "cro"])
        self.assertEqual(r[0]["published"], "2026-10-03"); self.assertEqual(r[0]["source_host"], "acme.example")
        a = feeds.parse(ATOM, "https://acme.example/blog/feed")
        self.assertEqual(a[0]["link"], "https://acme.example/blog/limits"); self.assertEqual(a[0]["published"], "2026-10-03")
        self.assertEqual(feeds.parse("<html>not a feed</html>"), [])

    def test_discovery(self):
        self.assertEqual(feeds.discover(PRICING, "https://acme.example/pricing"), ["https://acme.example/feed.xml"])


class News(unittest.TestCase):
    def test_bing_decode_and_merge_prefers_publisher_url(self):
        g = feeds.parse(GNEWS, ""); b = feeds.parse(BNEWS, "")
        self.assertEqual(g[0]["source_host"], "techcrunch.com")           # the outlet, not news.google.com
        merged = news.merge(g, b)
        raise_ = [m for m in merged if "raises" in m["title_key"]]
        self.assertEqual(len(raise_), 1)                                   # same story on both indexes: one item
        self.assertEqual(raise_[0]["publisher_url"], "https://techcrunch.com/2026/10/03/acme-raises/")
        self.assertEqual(raise_[0]["channel"], "bing")

    def test_keep_drops_forums_and_flags_unknown_hosts(self):
        g = feeds.parse(GNEWS, "")
        items = {i["id"]: dict(i, publisher_url=None) for i in g}
        self.assertEqual(news.keep(items["g2"], ["Acme"])[0], False)       # reddit: forum
        ok, flag = news.keep(items["g3"], ["Acme"])
        self.assertTrue(ok); self.assertEqual(flag, "unknown_host")        # names the entity, unknown outlet: kept, flagged
        self.assertFalse(news.keep(dict(items["g3"], title="Ten widget trends"), ["Acme"])[0])
        self.assertEqual(news.keep(items["g1"], ["Acme"])[1], "news")

    def test_normalize_title_strips_outlet_suffix(self):
        self.assertEqual(news.normalize_title("Acme raises $500M at a $5B valuation - TechCrunch"),
                         news.normalize_title("Acme raises $500M at a $5B valuation"))


class _Store:
    def __init__(self):
        self.d = {}

    def read(self, p):
        return self.d.get(p)

    def write(self, p, t, m=None):
        self.d[p] = t

    def update(self, p, tx, m=None, **k):
        new = tx(self.d.get(p))
        if new is None:
            return False
        self.d[p] = new
        return True

    def list(self, p, include_dirs=False):
        pre = p.rstrip("/") + "/"
        return sorted({k[len(pre):].split("/")[0] for k in self.d if k.startswith(pre)})


def _patch_store(st):
    from scout import selfserve
    return [mock.patch.object(selfserve, "read_data", st.read), mock.patch.object(selfserve, "write_data", st.write),
            mock.patch.object(selfserve, "update_data", st.update), mock.patch.object(selfserve, "list_data", st.list)]


class Events(unittest.TestCase):
    def setUp(self):
        self.st = _Store()
        self.p = _patch_store(self.st)
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_append_dedupes_and_consumes_per_card(self):
        f1 = events.make("acme", "news", "https://techcrunch.com/a", "Acme raises $500M", "The round values Acme at $5B.")
        f2 = events.make("acme", "news", "https://techcrunch.com/a", "Acme raises $500M", "dup")
        self.assertEqual(f1["fingerprint"], f2["fingerprint"])
        self.assertEqual(events.append("acme", [f1, f2]), 1)
        self.assertEqual(events.append("acme", [f1]), 0)
        self.assertEqual(len(events.open_for("acme", "card-a")), 1)
        self.assertEqual(events.consume("acme", "card-a", "t1", [f1["fingerprint"]]), 1)
        self.assertEqual(events.open_for("acme", "card-a"), [])
        self.assertEqual(len(events.open_for("acme", "card-b")), 1)      # a second card still sees it
        self.assertEqual(len(events.recent("acme")), 1)                 # the compare step sees everything


class Registry(unittest.TestCase):
    def test_entities_are_display_names_not_parents(self):
        self.assertEqual(registry.entity_key("Slack"), "slack")
        self.assertEqual(registry.entity_key("Google Cloud"), "google-cloud")
        self.assertEqual(registry.entity_key("Microsoft Teams"), "microsoft-teams")
        ents = registry.entities_for({"competitor": "Google", "my_company": "Perplexity"})
        self.assertEqual([(e["key"], e["role"]) for e in ents], [("google", "competitor"), ("perplexity", "my_company")])

    def test_weekly_sources_run_on_one_weekday(self):
        reg = {"sources": [{"url": "https://a.example/x", "cadence": "weekly"}, {"url": "https://a.example/y"}, {"url": "https://a.example/z", "cadence": "off"}]}
        days = [d for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10")
                if any(s["url"].endswith("/x") for s in registry.active_sources(reg, d))]
        self.assertEqual(len(days), 1)
        self.assertEqual([s["url"][-1] for s in registry.active_sources(reg, "2026-10-05") if s["url"].endswith("/y")], ["y"])


class Screen(unittest.TestCase):
    def _findings(self):
        return [dict(events.make("acme", "news", "https://techcrunch.com/a", "Acme raises $500M at a $5B valuation", "The round values Acme at $5B."), role="competitor"),
                dict(events.make("acme", "value_change", "https://acme.example/pricing", "pricing page: a figure changed", "BEFORE: $20 AFTER: $25"), role="competitor")]

    def test_validate_rejects_invented_sources_and_caps_at_five(self):
        f = self._findings()
        _, index = screen.build_user({"competitor": "Acme", "my_company": "Us"}, [], f, "2026-10-02")
        out = {"candidates": [{"finding_id": "f1", "signal": "Acme raises $500M on Oct 3", "subject_key": "NEW", "about": "competitor",
                               "valence": "back_foot", "substantial": True, "why_new": "new round"},
                              {"finding_id": "f99", "signal": "invented", "subject_key": "NEW", "about": "competitor", "valence": "back_foot", "substantial": True, "why_new": ""}]
                + [{"finding_id": "f2", "signal": f"dup {i}", "subject_key": "acme | pricing | team", "about": "competitor", "valence": "back_foot", "substantial": False, "why_new": ""} for i in range(6)]}
        cands = screen.validate(out, index)
        self.assertEqual(len(cands), 5)                                     # the cap, counted on the model's list
        self.assertEqual(cands[0]["source_hint"], "https://techcrunch.com/a")  # from the finding, not the model
        self.assertNotIn("invented", [c["signal"] for c in cands])
        self.assertEqual(cands[1]["source_hint"], "https://acme.example/pricing")
        self.assertEqual(cands[0]["finding_id"], f[0]["fingerprint"])

    def test_run_with_a_fake_client_records_cost_and_shape(self):
        class _Block:
            type = "tool_use"; name = "screen"
            input = {"candidates": [{"finding_id": "f1", "signal": "Acme raises $500M (Oct 3)", "subject_key": "NEW", "about": "competitor",
                                     "valence": "back_foot", "substantial": True, "why_new": "a new round"}]}

        class _Usage:
            input_tokens = 9000; output_tokens = 150; cache_read_input_tokens = 0; cache_creation_input_tokens = 0

        class _Msg:
            content = [_Block()]; usage = _Usage()

        class _Client:
            class messages:
                @staticmethod
                def create(**kw):
                    assert kw["tool_choice"] == {"type": "tool", "name": "screen"}
                    assert "FINDINGS" in kw["messages"][0]["content"]
                    return _Msg()
        with mock.patch.object(screen, "system_prompt", return_value="SYSTEM"), \
             mock.patch.object(screen.judgment, "require"), mock.patch.object(screen.judgment, "assert_clean"), \
             mock.patch.object(config, "FAST_MODEL", "claude-haiku-4-5-20251001"):
            r = screen.run({"competitor": "Acme"}, [{"subject_key": "acme | x", "claim": "old"}], self._findings(), "2026-10-02", client=_Client())
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["candidates"]), 1)
        self.assertTrue(r["candidates"][0]["substantial"])
        self.assertAlmostEqual(r["cost_usd"], 0.00975)                     # 9000 in + 150 out on Haiku 4.5 list prices

    def test_missing_block_skips_instead_of_failing(self):
        with mock.patch.object(screen, "system_prompt", return_value=None):
            r = screen.run({"competitor": "Acme"}, [], self._findings(), "2026-10-02")
        self.assertEqual(r["status"], "skipped")


class Compare(unittest.TestCase):
    def test_level_a_matches_excerpt_host_subject_and_names_the_miss_reason(self):
        findings = [events.make("acme", "news", "https://techcrunch.com/a", "Acme raises $500M", "The round values Acme at $5B post-money, led by Sequoia."),
                    events.make("acme", "page_change", "https://acme.example/pricing", "pricing", "New Enterprise tier at $60 per seat.")]
        screen_cands = [{"subject_key": "acme | cro | current", "substantial": True}]
        landed = [({"subject_key": "acme | valuation | current", "source_url": "https://www.reuters.com/x", "evidence_excerpt": "The round values Acme at $5B post-money, led by Sequoia."}, {}),
                  ({"subject_key": "acme | pricing | enterprise", "source_url": "https://acme.example/pricing", "evidence_excerpt": "something else entirely about a tier"}, {}),
                  ({"subject_key": "acme | cro | current", "source_url": "https://bloomberg.com/y", "evidence_excerpt": "Jane Doe becomes CRO effective immediately"}, {}),
                  ({"subject_key": "acme | layoffs | 2026", "source_url": "https://theinformation.com/z", "evidence_excerpt": "Acme cut 10% of staff"}, {}),
                  ({"subject_key": "acme | docs | limits", "source_url": "https://acme.example/docs/limits", "evidence_excerpt": "Rate limit is now 500 rpm"}, {})]
        rows = compare.level_a(landed, findings, screen_cands, watched_hosts={"acme.example"}, names=["Acme"])
        self.assertEqual(rows[2].get("covered_by"), "screen")                      # the screen named the subject
        self.assertEqual([r.get("miss_reason") for r in rows if r.get("miss")],
                         ["screened_out", "screened_out", "no_finding", "no_finding"])
        # row 0: a finding carried the excerpt; row 1: same URL as a finding; row 3: theinformation.com
        # is an indexed outlet with nothing new read; row 4: acme.example is watched, nothing read on docs
        rows2 = compare.level_a([({"subject_key": "x | y", "source_url": "https://randomblog.example/p", "evidence_excerpt": "z" * 40}, {})], [], [], set(), names=["Acme"])
        self.assertEqual(rows2[0]["miss_reason"], "no_source")

    def test_level_b_and_streak(self):
        triage = [{"signal": "Acme raises $500M Series D at a $5B valuation (Oct 3)", "subject_key": "NEW", "substantial": True},
                  {"signal": "Acme opens a Berlin office (Oct 2)", "subject_key": "NEW", "substantial": True},
                  {"signal": "minor blog post", "subject_key": "NEW", "substantial": False}]
        screen_cands = [{"signal": "Acme raises a $500M Series D round at a $5B valuation", "subject_key": "NEW", "substantial": True},
                        {"signal": "Acme pricing: Team seat moves from $20 to $25", "subject_key": "acme | pricing | team", "substantial": True}]
        lb = compare.level_b(triage, screen_cands, [], ["Acme"])
        self.assertEqual(len(lb["matched"]), 1); self.assertEqual(lb["misses"][0]["signal"][:10], "Acme opens")
        self.assertEqual(len(lb["screen_only"]), 1)
        st = _Store()
        with mock.patch.object(compare.selfserve, "read_data", st.read), mock.patch.object(compare.selfserve, "write_data", st.write):
            s1 = compare.update_streak("2026-10-06", [{"slug": "a", "misses_a": 0, "errors": 0, "sources": 10, "screen_cost": 0.01, "screen_subst": 1, "triage_subst": 2, "findings": 2}], gate_runs=7, write=True)
            self.assertEqual(s1["clean_streak"], 1); self.assertFalse(s1["ready"])
            s2 = compare.update_streak("2026-10-07", [{"slug": "a", "misses_a": 1, "errors": 0, "sources": 10, "screen_cost": 0.01, "screen_subst": 1, "triage_subst": 2, "findings": 2}], gate_runs=7, write=True)
            self.assertEqual(s2["clean_streak"], 0); self.assertIn("1 Level A miss(es)", s2["runs"][-1]["reasons"])
            s3 = compare.update_streak("2026-10-08", [{"slug": "a", "misses_a": 0, "errors": 0, "sources": 10, "screen_cost": 0.05, "screen_subst": 3, "triage_subst": 1, "findings": 2}], gate_runs=7, write=True)
            self.assertEqual(s3["clean_streak"], 0); self.assertEqual(s3["runs"][-1]["reasons"], ["screen $0.050 a card"])   # better recall than triage is not a failure
            self.assertEqual(s3["runs"][-1]["screen_escalating_cards"], 1)
            # a baseline day (no findings anywhere) neither counts nor breaks the streak
            s4 = compare.update_streak("2026-10-09", [{"slug": "a", "misses_a": 0, "errors": 0, "sources": 10, "screen_cost": 0.01, "screen_subst": 0, "triage_subst": 1, "findings": 0, "findings_recent": 0}], gate_runs=7, write=True)
            self.assertTrue(s4["runs"][-1]["baseline"]); self.assertEqual(s4["clean_streak"], 0)
            s5 = compare.update_streak("2026-10-10", [{"slug": "a", "misses_a": 0, "errors": 0, "sources": 10, "screen_cost": 0.01, "screen_subst": 1, "triage_subst": 1, "findings": 3, "findings_recent": 3}], gate_runs=7, write=True)
            self.assertEqual(s5["clean_streak"], 1)


class MonitorModes(unittest.TestCase):
    """The monitor with sensors in shadow and in gate, every model and network call faked."""

    def setUp(self):
        from scout import monitor
        self.monitor = monitor
        self.st = _Store()
        self.p = _patch_store(self.st) + [
            mock.patch.object(config, "PROPAGATE_MODE", "off"), mock.patch.object(config, "SIGNALS_ENABLED", False),
            mock.patch.object(config, "SHADOW_EVAL_ENABLED", False), mock.patch.object(config, "CALL_CAPTURE_ENABLED", False),
            mock.patch.object(config, "RUN_MAX_USD", 0.0), mock.patch.object(config, "AUDIENCE_LEADS", False),
            mock.patch.object(monitor.store, "load_meta", return_value={"competitor": "Acme", "my_company": "Us", "last_checked": "2026-10-03T11:00:00"}),
            mock.patch.object(monitor.store, "load_claims", return_value=[{"id": "c1", "subject_key": "acme | pricing | team", "claim": "Team is $20", "section": "pricing"}]),
            mock.patch.object(monitor.store, "write_baseline"), mock.patch.object(monitor, "_append_alerts"),
            mock.patch.object(monitor, "_current_md", return_value="# card"),
        ]
        for x in self.p:
            x.start()
        monitor._SENSORS.update({"summary": {"acme": {"pages_checked": 3, "pages_changed": 1, "feed_items": 0, "news_hits": 2, "findings": 3, "unavailable": False},
                                             "us": {"pages_checked": 1, "pages_changed": 0, "feed_items": 0, "news_hits": 0, "findings": 0, "unavailable": False}},
                                 "today": "2026-10-06", "error": None})
        self.st.write("sensors/acme/registry.json", json.dumps({"entity": "acme", "name": "Acme", "sources": [{"url": "https://acme.example/pricing", "kind": "pricing"}]}))
        self.st.write("sensors/us/registry.json", json.dumps({"entity": "us", "name": "Us", "sources": []}))
        events.append("acme", [events.make("acme", "value_change", "https://acme.example/pricing", "pricing page: a figure changed", "BEFORE: Team $20 AFTER: Team $25")])

    def tearDown(self):
        for x in reversed(self.p):
            x.stop()
        self.monitor._SENSORS.update({"summary": {}, "today": None, "error": None})

    def _triage(self, cands):
        async def _f(*a, **k):
            return {"text": json.dumps({"has_candidates": bool(cands), "candidates": cands}), "cost_usd": 0.15}
        return _f

    def _screen(self, cands):
        return lambda meta, claims, findings, cutoff, sig_block="": {"candidates": cands, "cost_usd": 0.012, "status": "ok", "detail": f"{len(cands)} candidate(s)"}

    def test_shadow_runs_screen_next_to_triage_and_writes_the_compare_record(self):
        screen_c = [{"signal": "Team seat $20 -> $25 (Oct 6)", "subject_key": "acme | pricing | team", "about": "competitor", "valence": "back_foot",
                     "substantial": True, "why_new": "new price", "source_hint": "https://acme.example/pricing", "finding_id": "x"}]
        with mock.patch.object(config, "SENSORS_MODE", "shadow"), \
             mock.patch.object(self.monitor, "_run_triage", self._triage([])) as tri, \
             mock.patch("scout.sensors.screen.run", self._screen(screen_c)):
            res = self.monitor.check("card-a", write=True)
        rows = {r["step"]: r for r in res["steps"]}
        self.assertEqual(rows["sensors"]["status"], "ran"); self.assertEqual(rows["screen"]["status"], "ran")
        self.assertEqual(rows["triage"]["status"], "ran")                          # shadow: triage still decides
        self.assertTrue(res["no_change"])                                           # the screen's candidate did NOT escalate
        self.assertEqual(rows["compare"]["status"], "ran")
        self.assertAlmostEqual(res["cost"]["screen"], 0.012)
        recs = [k for k in self.st.d if k.startswith("sensors/_compare/") and k.endswith("/card-a.json")]
        self.assertEqual(len(recs), 1)
        doc = json.loads(self.st.d[recs[0]])
        self.assertEqual(doc["mode"], "shadow"); self.assertEqual(len(doc["screen"]["candidates"]), 1)
        self.assertEqual(doc["level_b"]["screen_only"][0]["subject_key"], "acme | pricing | team")
        self.assertEqual(events.open_for("acme", "card-a"), [])                      # consumed for this card
        self.assertEqual(len(events.open_for("acme", "card-b")), 1)

    def test_gate_uses_the_screen_and_skips_triage(self):
        screen_c = [{"signal": "Team seat $20 -> $25 (Oct 6)", "subject_key": "acme | pricing | team", "about": "competitor", "valence": "back_foot",
                     "substantial": True, "why_new": "new price", "source_hint": "https://acme.example/pricing", "finding_id": "x"}]
        arm = mock.Mock(side_effect=lambda slug, meta, since, substantial, claims, result, sig_block="": (result["cost"].__setitem__("materiality", 0.3) or ([], {"kept": [], "cut": [], "results": []}, [])))
        with mock.patch.object(config, "SENSORS_MODE", "gate"), mock.patch.object(config, "SENSOR_SWEEP", False), \
             mock.patch.object(self.monitor, "_run_triage", side_effect=AssertionError("gate: triage must not run")), \
             mock.patch("scout.sensors.screen.run", self._screen(screen_c)), \
             mock.patch.object(self.monitor, "_competitor_arm", arm):
            res = self.monitor.check("card-a", write=True)
        rows = {r["step"]: r for r in res["steps"]}
        self.assertEqual(rows["triage"]["status"], "skipped")
        self.assertEqual(arm.call_args.args[3][0]["subject_key"], "acme | pricing | team")   # the screen's candidate escalated
        self.assertEqual(res["cost"]["triage"], 0.0)

    def test_gate_falls_back_to_triage_on_held_window_dispatch_and_outage(self):
        cases = [
            ("held window", {"unresolved_since": "2026-10-01"}, {}, "scheduled"),
            ("dispatched run", {}, {}, "signal"),
            ("news channel down", {}, {"unavailable": True}, "scheduled"),
        ]
        for name, meta_extra, sum_extra, reason in cases:
            meta = {"competitor": "Acme", "my_company": "Us", "last_checked": "2026-10-03T11:00:00", **meta_extra}
            self.monitor._SENSORS["summary"]["acme"] = {**self.monitor._SENSORS["summary"]["acme"], **sum_extra}
            env = {"SCOUT_MONITOR_REASON": "8-K"} if reason == "signal" else {}
            with mock.patch.object(config, "SENSORS_MODE", "gate"), mock.patch.object(config, "SENSOR_SWEEP", False), \
                 mock.patch.object(self.monitor.store, "load_meta", return_value=meta), \
                 mock.patch.dict("os.environ", env, clear=False), \
                 mock.patch.object(self.monitor, "_run_triage", self._triage([])) as tri, \
                 mock.patch("scout.sensors.screen.run", self._screen([])):
                res = self.monitor.check("card-a", write=False)
            rows = {r["step"]: r for r in res["steps"]}
            self.assertEqual(rows["triage"]["status"], "ran", name)
            self.monitor._SENSORS["summary"]["acme"]["unavailable"] = False

    def test_gate_sweep_day_runs_both_and_widens_the_cutoff(self):
        slug = "card-a"
        day = next(d for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10") if self.monitor._sweep_day(slug, d))
        self.monitor._SENSORS["today"] = day
        seen = {}

        async def tri(meta, since, claims, my_since=None, extra="", **kw):
            seen["since"] = since
            return {"text": json.dumps({"has_candidates": True, "candidates": [{"signal": "Acme opens Berlin office", "subject_key": "NEW", "about": "competitor", "substantial": True}]}), "cost_usd": 0.15}
        screen_c = [{"signal": "Team seat $20 -> $25", "subject_key": "acme | pricing | team", "about": "competitor", "valence": "back_foot",
                     "substantial": True, "why_new": "", "source_hint": "https://acme.example/pricing", "finding_id": "x"}]
        arm = mock.Mock(side_effect=lambda slug, meta, since, substantial, claims, result, sig_block="": ([], {"kept": [], "cut": [], "results": []}, []))
        meta = {"competitor": "Acme", "my_company": "Us", "last_checked": f"{day}T11:00:00"}
        with mock.patch.object(config, "SENSORS_MODE", "gate"), mock.patch.object(config, "SENSOR_SWEEP", True), \
             mock.patch.object(self.monitor.store, "load_meta", return_value=meta), \
             mock.patch.object(self.monitor, "_run_triage", tri), mock.patch("scout.sensors.screen.run", self._screen(screen_c)), \
             mock.patch.object(self.monitor, "_competitor_arm", arm), \
             mock.patch.object(self.monitor, "datetime", wraps=self.monitor.datetime) as dt:
            dt.now.return_value = self.monitor.datetime.fromisoformat(f"{day}T11:05:00")
            res = self.monitor.check(slug, write=True)
        self.assertLess(seen["since"], day)                                           # the sweep looks back a week
        subs = sorted(c["subject_key"] for c in arm.call_args.args[3])
        self.assertEqual(subs, ["NEW", "acme | pricing | team"])                          # unioned
        self.assertIn("sweep day", {r["step"]: r for r in res["steps"]}["triage"]["detail"])

    def test_off_is_byte_identical(self):
        with mock.patch.object(config, "SENSORS_MODE", "off"), mock.patch.object(self.monitor, "_run_triage", self._triage([])), \
             mock.patch("scout.sensors.screen.run", side_effect=AssertionError("off: the screen must not run")):
            res = self.monitor.check("card-a", write=False)
        self.assertNotIn("sensors", res)
        self.assertFalse(any(r["step"] in ("sensors", "screen", "compare") for r in res["steps"]))


class SweepDay(unittest.TestCase):
    def test_deterministic_and_within_run_weekdays(self):
        from scout import monitor
        days = [d for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11") if monitor._sweep_day("some__vs__card__x", d)]
        self.assertEqual(len(days), 1)
        self.assertNotEqual(days[0], "2026-10-11")                                      # never Sunday


if __name__ == "__main__":
    unittest.main()


class Seeding(unittest.TestCase):
    """scripts/seed_sensors.py: the registry planner's deterministic parts (no network)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("seed_sensors", os.path.join(config.APP_ROOT, "scripts", "seed_sensors.py"))
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_main_host_prefers_the_name_and_the_registrable_domain(self):
        from collections import Counter
        m = self.m
        self.assertEqual(m._main_host(Counter({"developers.openai.com": 2, "status.openai.com": 1}), "OpenAI"), "openai.com")
        self.assertEqual(m._main_host(Counter({"salesforce.com": 6, "slack.com": 4}), "Slack"), "slack.com")
        self.assertEqual(m._main_host(Counter({"cloud.google.com": 4, "blog.google": 2}), "Google Cloud"), "cloud.google.com")
        self.assertEqual(m._main_host(Counter({"docs.perplexity.ai": 1}), "Perplexity"), "perplexity.ai")

    def test_company_pages_are_strict_and_look_alikes_are_suspicious(self):
        m = self.m
        self.assertEqual(m._is_company_page("www.salesforceben.com", "Slack"), (False, True))
        self.assertEqual(m._is_company_page("9to5google.com", "Google"), (False, True))
        self.assertEqual(m._is_company_page("www.aboutamazon.com", "AWS"), (True, False))
        self.assertEqual(m._is_company_page("slack.com", "Slack"), (True, False))
        self.assertEqual(m._is_company_page("techcrunch.com", "Slack"), (False, False))

    def test_queries_are_specific_for_broad_names_and_products_do_not_inherit(self):
        m = self.m
        self.assertEqual(m._queries("Google", [{"focus": None}]), ['"Gemini"', '"DeepMind"', '"Google" AI'])
        self.assertEqual(m._queries("Microsoft Teams", [{"focus": None}]), ['"Microsoft Teams"'])
        self.assertEqual(m._queries("Anthropic", [{"focus": "enterprise coding developers"}]), ['"Anthropic"', '"Claude"', '"Anthropic" enterprise coding developers'])

    def test_every_focused_card_keeps_its_own_query(self):
        """Two cards on one company: each focus area gets its own query; neither is merged or cut (10/3 bug)."""
        m = self.m
        q = m._queries("OpenAI", [{"focus": "Enterprise (sovereign), developers, agents"},
                                  {"focus": "Enterprise collaboration agents inside Slack and Teams"},
                                  {"focus": None}])
        self.assertIn('"OpenAI" enterprise sovereign developers agents', q)
        self.assertIn('"OpenAI" enterprise collaboration agents inside slack teams', q)
        self.assertEqual(q[0], '"OpenAI"')

    def test_section_page_collapses_permalinks(self):
        m = self.m
        self.assertEqual(m._section_page("https://www.anthropic.com/news/claude-5"), "https://www.anthropic.com/news")
        self.assertEqual(m._section_page("https://techcrunch.com/2026/10/03/story"), "https://techcrunch.com/")


class RenderedTier(unittest.TestCase):
    """The three-tier page read: plain, then the headless browser, then `challenge` (no browser is
    launched here: the renderer is faked)."""

    def _plain(self, status=200, text=PRICING, error=None):
        return lambda url, etag=None, lm=None: {"status": status, "text": text, "etag": None, "last_modified": None, "error": error, "unchanged": False}

    def test_plain_read_with_text_stays_plain(self):
        from scout.sensors import collect, rendered
        src, pst = {"url": "https://acme.example/pricing"}, {}
        with mock.patch.object(collect, "fetch", self._plain()), mock.patch.object(rendered, "fetch", side_effect=AssertionError("must not render")), \
             mock.patch.object(config, "SENSOR_RENDERED", True):
            r, blocks, tier = collect.read_page(src["url"], pst, src)
        self.assertEqual(tier, "plain"); self.assertGreaterEqual(len(blocks), 3); self.assertNotIn("read", src)

    def test_blocked_or_thin_falls_to_the_browser_and_remembers_it(self):
        from scout.sensors import collect, rendered
        src, pst = {"url": "https://acme.example/pricing"}, {}
        with mock.patch.object(collect, "fetch", self._plain(status=403, text=None, error="HTTP 403")), \
             mock.patch.object(rendered, "fetch", return_value={"status": 200, "text": PRICING, "error": None, "challenge": False, "title": "Pricing"}), \
             mock.patch.object(config, "SENSOR_RENDERED", True):
            r, blocks, tier = collect.read_page(src["url"], pst, src)
        self.assertEqual(tier, "rendered"); self.assertGreaterEqual(len(blocks), 3); self.assertEqual(src["read"], "rendered")
        # a JavaScript shell (200 with no text) takes the same path
        src2 = {"url": "https://acme.example/app"}
        with mock.patch.object(collect, "fetch", self._plain(text="<html><body><div id=root></div></body></html>")), \
             mock.patch.object(rendered, "fetch", return_value={"status": 200, "text": PRICING, "error": None, "challenge": False, "title": ""}), \
             mock.patch.object(config, "SENSOR_RENDERED", True):
            _, blocks2, tier2 = collect.read_page(src2["url"], {}, src2)
        self.assertEqual(tier2, "rendered")

    def test_bot_wall_is_a_challenge_not_a_finding(self):
        from scout.sensors import collect, rendered
        src = {"url": "https://walled.example/pricing"}
        with mock.patch.object(collect, "fetch", self._plain(status=403, text=None, error="HTTP 403")), \
             mock.patch.object(rendered, "fetch", return_value={"status": 403, "text": None, "error": "bot wall (challenge page)", "challenge": True, "title": "Just a moment..."}), \
             mock.patch.object(config, "SENSOR_RENDERED", True):
            r, blocks, tier = collect.read_page(src["url"], {}, src)
        self.assertEqual(tier, "challenge"); self.assertEqual(src["read"], "challenge"); self.assertIn("bot wall", r["error"])

    def test_renderer_off_keeps_the_plain_failure(self):
        from scout.sensors import collect, rendered
        src = {"url": "https://acme.example/pricing"}
        with mock.patch.object(collect, "fetch", self._plain(status=403, text=None, error="HTTP 403")), \
             mock.patch.object(rendered, "fetch", side_effect=AssertionError("renderer off")), \
             mock.patch.object(config, "SENSOR_RENDERED", False):
            r, blocks, tier = collect.read_page(src["url"], {}, src)
        self.assertEqual(tier, "failed")

    def test_challenge_markers(self):
        from scout.sensors import rendered
        self.assertTrue(rendered.is_challenge("<html><title>Just a moment...</title></html>"))
        self.assertTrue(rendered.is_challenge("<p>Please verify you are a human</p>"))
        self.assertFalse(rendered.is_challenge("<html><title>Pricing</title><p>Team plan $20</p></html>", "Pricing"))


class EvidenceInHand(unittest.TestCase):
    """Part 3 #4 (gate mode): page findings carry the text code read; a paid step whose every candidate
    carries it runs with the lower turn cap and the evidence note, and the capture flags the cell."""

    def test_only_page_findings_carry_evidence(self):
        f_page = dict(events.make("acme", "value_change", "https://acme.example/pricing", "pricing: a figure changed", "BEFORE: $20 AFTER: $25"), role="competitor")
        f_news = dict(events.make("acme", "news", "https://techcrunch.com/a", "Acme raises $500M", "The round values Acme at $5B."), role="competitor")
        _, index = screen.build_user({"competitor": "Acme"}, [], [f_page, f_news], "2026-10-02")
        out = {"candidates": [{"finding_id": "f1", "signal": "price up", "subject_key": "NEW", "about": "competitor", "valence": "back_foot", "substantial": True, "why_new": ""},
                              {"finding_id": "f2", "signal": "round", "subject_key": "NEW", "about": "competitor", "valence": "back_foot", "substantial": True, "why_new": ""}]}
        cands = screen.validate(out, index)
        self.assertIn("evidence", cands[0]); self.assertIn("$25", cands[0]["evidence"])
        self.assertNotIn("evidence", cands[1])

    def test_cap_and_note_only_when_every_candidate_has_evidence(self):
        from scout import monitor, calllog
        with mock.patch.object(monitor.judgment, "optional", return_value="NOTE"):
            note, cap = monitor._evidence_in_hand([{"signal": "a", "evidence": "x"}, {"signal": "b", "evidence": "y"}])
            self.assertEqual(cap, config.EVIDENCE_MAX_TURNS); self.assertIn("NOTE", note)
            self.assertTrue(calllog._CTX.get("evidence_attached"))
            note2, cap2 = monitor._evidence_in_hand([{"signal": "a", "evidence": "x"}, {"signal": "b"}])
            self.assertEqual((note2, cap2), ("", config.MAX_TURNS)); self.assertNotIn("evidence_attached", calllog._CTX)
            self.assertEqual(monitor._evidence_in_hand([]), ("", config.MAX_TURNS))

    def test_how_panel_copy_follows_the_mode(self):
        from scout import page
        with mock.patch.object(config, "SENSORS_MODE", "gate"):
            h = page._how_panel()
        self.assertIn("Code reads each company", h); self.assertIn("Code sensors read the sources", h); self.assertNotIn("Scans each competitor", h)
        with mock.patch.object(config, "SENSORS_MODE", "shadow"):
            h = page._how_panel()
        self.assertIn("Scans each competitor", h); self.assertNotIn("Code sensors read the sources", h)
