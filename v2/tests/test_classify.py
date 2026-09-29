"""scout/sources/classify.py (WS0): the deterministic source-class map and the conservative tier
normalization. Pure; no network."""
import unittest

from scout import schema
from scout.sources import classify as cl


class Map(unittest.TestCase):
    def test_known_classes(self):
        cases = {
            "https://www.sec.gov/Archives/edgar/data/1108524/000110852426000068/crm-20260731.htm": "filing",
            "https://data.sec.gov/api/xbrl/companyconcept/CIK0001108524/us-gaap/Revenues.json": "filing",
            "https://www.courtlistener.com/docket/123/x/": "court",
            "https://www.ftc.gov/news-events/press-releases/2026/x": "government",
            "https://ec.europa.eu/commission/presscorner/x": "government",
            "https://www.prnewswire.com/news-releases/x.html": "company_statement",
            "https://ir.hubspot.com/news/x": "company_statement",
            "https://boards.greenhouse.io/anthropic/jobs/123": "job_posting",
            "https://jobs.ashbyhq.com/openai/abc": "job_posting",
            "https://web.archive.org/web/20260901/https://claude.com/pricing": "page_snapshot",
            "https://www.cnbc.com/2026/09/01/x.html": "news",
            "https://techcrunch.com/2026/09/01/x/": "news",
            "https://finance.yahoo.com/news/x": "news",
            "https://arxiv.org/abs/2609.00006": "research",
            "https://www.g2.com/products/x/reviews": "review_site",
            "https://learn.g2.com/x": "review_site",
            "https://news.ycombinator.com/item?id=1": "forum",
            "https://community.atlassian.com/t5/x": "forum",
            "https://www.reddit.com/r/x/": "forum",
            "https://marc0.dev/x": "unknown",
            "https://medium.com/@x/y": "unknown",
            "not a url": "unknown", "": "unknown", None: "unknown",
        }
        for url, want in cases.items():
            self.assertEqual(cl.classify(url), want, url)

    def test_company_statement_from_the_card_names(self):
        self.assertEqual(cl.classify("https://www.anthropic.com/news/x", "Anthropic", "OpenAI"), "company_statement")
        self.assertEqual(cl.classify("https://cloud.google.com/blog/x", "Google Cloud", "AWS"), "company_statement")
        self.assertEqual(cl.classify("https://aws.amazon.com/blogs/x", "Google Cloud", "AWS"), "company_statement")
        self.assertEqual(cl.classify("https://cognition.ai/blog/x", "Cursor", "Cognition"), "company_statement")
        self.assertEqual(cl.classify("https://careers.notion.com/x", "Notion", "Atlassian"), "job_posting")
        # the same host without the names is unknown: no guessing
        self.assertEqual(cl.classify("https://www.anthropic.com/news/x"), "unknown")
        # a generic token never matches (the "ai"/"cloud"/"team" stop words)
        self.assertEqual(cl.classify("https://unite.ai/x", "Mistral AI", "OpenAI"), "news")
        self.assertEqual(cl.classify("https://x.cloud.example/x", "Google Cloud", None), "unknown")

    def test_host_of(self):
        self.assertEqual(cl.host_of("https://WWW.CNBC.com/x"), "cnbc.com")
        self.assertEqual(cl.host_of("garbage"), "")

    def test_labels_cover_every_class(self):
        for c in cl.SOURCE_CLASSES:
            self.assertIn(c, cl.CLASS_LABEL); self.assertIn(c, cl.CLASS_TIER)
        self.assertEqual(schema.SOURCE_CLASSES, cl.SOURCE_CLASSES)


class Stamp(unittest.TestCase):
    META = {"competitor": "OpenAI", "my_company": "Anthropic"}

    def _fact(self, url, tier="reputable_secondary", ctype="fact"):
        return {"id": "c_0123456789ab", "subject_key": "k", "claim": "x", "claim_type": ctype, "section": "recent_moves",
                "zone": None, "order": 0, "verified": True, "confidence": "high", "source_url": url,
                "source_tier": tier, "evidence_excerpt": "e" * 40}

    def test_known_class_normalizes_tier(self):
        c = cl.stamp(self._fact("https://www.sec.gov/Archives/x.htm", tier="reputable_secondary"), self.META)
        self.assertEqual((c["source_class"], c["source_tier"]), ("filing", "primary"))
        c = cl.stamp(self._fact("https://openai.com/index/x", tier="reputable_secondary"), self.META)
        self.assertEqual((c["source_class"], c["source_tier"]), ("company_statement", "primary"))
        c = cl.stamp(self._fact("https://www.cnbc.com/x", tier="primary"), self.META)
        self.assertEqual((c["source_class"], c["source_tier"]), ("news", "reputable_secondary"))

    def test_unknown_keeps_the_model_tier(self):
        c = cl.stamp(self._fact("https://marc0.dev/x", tier="reputable_secondary"), self.META)
        self.assertEqual((c["source_class"], c["source_tier"]), ("unknown", "reputable_secondary"))
        self.assertNotIn("source_class_conflict", c)

    def test_never_invalidates_a_fact(self):
        c = cl.stamp(self._fact("https://news.ycombinator.com/item?id=1", tier="reputable_secondary"), self.META)
        self.assertEqual(c["source_class"], "forum")
        self.assertEqual(c["source_tier"], "reputable_secondary")          # unchanged: fact on sentiment
        self.assertEqual(c["source_class_conflict"], "forum")
        self.assertEqual(schema.validation_errors({**c, "grounding": {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-09-28"}}), [])
        s = cl.stamp(self._fact("https://news.ycombinator.com/item?id=1", tier="reputable_secondary", ctype="sentiment"), self.META)
        self.assertEqual(s["source_tier"], "sentiment_only"); self.assertNotIn("source_class_conflict", s)

    def test_provenance_and_corroboration_get_classes(self):
        c = self._fact("https://www.cnbc.com/x")
        c["provenance"] = {"source_url": "https://www.anthropic.com/news/x", "source_tier": "reputable_secondary"}
        c["corroboration"] = [{"source_url": "https://www.reuters.com/x", "source_tier": "reputable_secondary", "note": "n", "grounded": False}]
        cl.stamp(c, self.META)
        self.assertEqual(c["provenance"]["source_class"], "company_statement")
        self.assertEqual(c["provenance"]["source_tier"], "primary")
        self.assertEqual(c["corroboration"][0]["source_class"], "news")

    def test_derived_claim_without_source_is_left_alone(self):
        d = {"id": "c_2", "claim": "x", "claim_type": "interpretation", "derived_from": "c_1"}
        self.assertEqual(cl.stamp(dict(d), self.META), d)

    def test_never_raises(self):
        self.assertEqual(cl.stamp({"source_url": object()}, None)["source_url"].__class__, object)

    def test_class_counts(self):
        rows = [cl.stamp(self._fact("https://www.cnbc.com/x"), self.META),
                cl.stamp(self._fact("https://www.sec.gov/x"), self.META),
                {**cl.stamp(self._fact("https://www.cnbc.com/y"), self.META), "status": "retired"},
                {"id": "c_9", "claim": "derived", "derived_from": "c_1"}]
        self.assertEqual(cl.class_counts(rows), {"news": 1, "filing": 1})


if __name__ == "__main__":
    unittest.main()
