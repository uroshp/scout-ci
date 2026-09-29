"""scout/sources (WS1, 2026-09-28): the EDGAR / job-board / Wayback clients on recorded fixtures,
the registry, the shared tool spec, the two adapters' parity, XBRL grounding (exact or nothing),
the flag gating, and grounding's model-free purity. No network."""
import ast
import json
import os
import unittest
from unittest import mock

from scout import config, grounding, localagent, monitor, schema, sources_tool
from scout.sources import classify, edgar, jobs, registry, toolspec, wayback

CONCEPT_JSON = {"cik": 1108524, "taxonomy": "us-gaap", "tag": "Revenues", "entityName": "Salesforce, Inc.",
                "units": {"USD": [
                    {"end": "2026-07-31", "val": 11345000000, "fy": 2027, "fp": "Q2", "form": "10-Q", "filed": "2026-08-27", "accn": "0001108524-26-000190", "start": "2026-05-01"},
                    {"end": "2026-07-31", "val": 11345000000, "fy": 2027, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01", "accn": "0001108524-26-000100", "start": "2026-05-01"},
                    {"end": "2026-04-30", "val": 11133000000, "fy": 2027, "fp": "Q1", "form": "10-Q", "filed": "2026-05-28", "accn": "0001108524-26-000127"},
                    {"end": "2026-01-31", "val": 42000000000, "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-03-05", "accn": "0001108524-26-000050"},
                    {"end": "2026-01-31", "val": 42000000000, "fy": 2026, "fp": "FY", "form": "8-K", "filed": "2026-02-26", "accn": "0001108524-26-000020"}]}}

SUBMISSIONS_JSON = {"name": "HUBSPOT INC", "filings": {"recent": {
    "form": ["8-K", "4", "10-Q", "8-K"], "filingDate": ["2026-08-05", "2026-08-04", "2026-08-06", "2026-05-07"],
    "accessionNumber": ["0001193125-26-335148", "0001193125-26-335000", "0001404655-26-000042", "0001193125-26-211923"],
    "primaryDocument": ["hubs-20260803.htm", "x.xml", "hubs-20260630.htm", "hubs-20260507.htm"],
    "primaryDocDescription": ["8-K", "", "10-Q", "8-K"], "items": ["2.02,9.01", "", "", "2.02"]}}}


class _Resp:
    def __init__(self, body, status=200, text=None):
        self._b, self.status_code = body, status
        self.text = text if text is not None else json.dumps(body)
        self.headers = {"content-type": "application/json"}
        self.url = "https://data.sec.gov/x.json"

    def json(self):
        return self._b


class Edgar(unittest.TestCase):
    def test_canonical_line_and_lines(self):
        line = edgar.canonical_line("us-gaap", "Revenues", "USD", CONCEPT_JSON["units"]["USD"][0])
        self.assertEqual(line, "us-gaap:Revenues end=2026-07-31 value=11345000000 unit=USD form=10-Q fy=2027 fp=Q2 filed=2026-08-27 accn=0001108524-26-000190")
        self.assertEqual(len(edgar.canonical_lines(CONCEPT_JSON)), 5)
        facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": CONCEPT_JSON["units"]["USD"][:2]}}}}}
        self.assertEqual(len(edgar.canonical_lines(facts)), 2)

    def test_fact_dedupes_to_latest_filing_and_filters_forms(self):
        with mock.patch.object(edgar, "_get_json", return_value=CONCEPT_JSON):
            r = edgar.fact(1108524, "Revenues", limit=8)
        self.assertEqual([x["end"] for x in r["rows"]], ["2026-07-31", "2026-04-30", "2026-01-31"])
        self.assertEqual(r["rows"][0]["accn"], "0001108524-26-000190")     # the later filing wins
        self.assertEqual(r["rows"][2]["form"], "10-K")                      # the 8-K duplicate is filtered
        self.assertTrue(r["url"].endswith("/us-gaap/Revenues.json"))
        self.assertIn(r["rows"][0]["line"], edgar.render_fact_text(r))

    def test_fact_any_tries_concepts_in_order(self):
        calls = []
        def fake(cik, concept, **kw):
            calls.append(concept)
            return {"rows": [] if concept != "Revenues" else [{"end": "x"}], "url": "u", "concept": concept, "unit": "USD", "name": "n"}
        with mock.patch.object(edgar, "fact", side_effect=fake):
            r = edgar.fact_any(1, "revenue")
        self.assertEqual(calls, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"])
        self.assertEqual(r["concept"], "Revenues")
        self.assertEqual(edgar.resolve_concepts("NetIncomeLoss"), ["NetIncomeLoss"])
        self.assertEqual(edgar.resolve_concepts(""), [])

    def test_filings_rows(self):
        with mock.patch.object(edgar, "_get_json", return_value=SUBMISSIONS_JSON):
            rows = edgar.filings(1404655, forms=("8-K", "10-Q"), since="2026-06-01", limit=10)
        self.assertEqual([r["form"] for r in rows], ["8-K", "10-Q"])
        self.assertEqual(rows[0]["url"], "https://www.sec.gov/Archives/edgar/data/1404655/000119312526335148/hubs-20260803.htm")
        self.assertEqual(rows[0]["items"], "2.02,9.01")
        self.assertIn("HUBSPOT INC", edgar.render_filings_text(rows, "HUBSPOT INC"))

    def test_xbrl_url_detection(self):
        self.assertTrue(edgar.is_xbrl_url("https://data.sec.gov/api/xbrl/companyconcept/CIK0001108524/us-gaap/Revenues.json"))
        self.assertTrue(edgar.is_xbrl_url("https://data.sec.gov/api/xbrl/companyfacts/CIK0001108524.json"))
        self.assertFalse(edgar.is_xbrl_url("https://www.sec.gov/Archives/x.htm"))


class Jobs(unittest.TestCase):
    def test_normalize_three_hosts(self):
        gh = {"jobs": [{"id": 1, "title": "AE", "location": {"name": "NYC"}, "departments": [{"name": "Sales"}], "updated_at": "2026-09-01T00:00:00Z", "absolute_url": "u1"}]}
        ab = {"jobs": [{"id": "a", "title": "RE", "location": "SF", "department": "Research", "publishedAt": "2026-09-02T00:00:00Z", "jobUrl": "u2"}]}
        lv = [{"id": "l", "text": "PM", "categories": {"location": "Paris", "team": "Product"}, "createdAt": 1758000000000, "hostedUrl": "u3"}]
        self.assertEqual(jobs.normalize("greenhouse", gh)[0], {"id": "1", "title": "AE", "location": "NYC", "department": "Sales", "updated_at": "2026-09-01", "url": "u1"})
        self.assertEqual(jobs.normalize("ashby", ab)[0]["department"], "Research")
        self.assertEqual(jobs.normalize("lever", lv)[0]["updated_at"], "2025-09-16")     # 1758000000000 ms
        with self.assertRaises(ValueError):
            jobs.board_url("workday", "x")

    def test_diff_and_summary(self):
        prev = [{"id": "1", "department": "Sales", "title": "AE"}, {"id": "2", "department": "Eng", "title": "SWE"}]
        cur = [{"id": "2", "department": "Eng", "title": "SWE"}, {"id": "3", "department": "Sales", "title": "AE2"}, {"id": "4", "department": "Sales", "title": "AE3"}]
        d = jobs.diff(prev, cur)
        self.assertEqual(([r["id"] for r in d["added"]], [r["id"] for r in d["removed"]], d["net"]), (["3", "4"], ["1"], 1))
        self.assertEqual(d["by_department"], {"Sales": 1})
        txt = jobs.summarize([{**r, "updated_at": "2026-09-0%d" % i, "location": "", "url": ""} for i, r in enumerate(cur, 1)])
        self.assertIn("3 open postings", txt); self.assertIn("Sales 2", txt)
        self.assertEqual(jobs.summarize([]), "No open postings on the board.")


class Wayback(unittest.TestCase):
    def test_snapshots_parse_and_order(self):
        body = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                ["com,claude)/pricing", "20260101044440", "https://claude.com/pricing", "text/html", "200", "D1", "225189"],
                ["com,claude)/pricing", "20260901120000", "https://claude.com/pricing", "text/html", "200", "D2", "230000"]]
        with mock.patch.object(wayback, "_spaced"), mock.patch("scout.grounding._fetch_response", return_value=_Resp(body)):
            rows = wayback.snapshots("https://claude.com/pricing", since="2026")
        self.assertEqual([r["date"] for r in rows], ["2026-09-01", "2026-01-01"])
        self.assertEqual(rows[0]["snapshot_url"], "https://web.archive.org/web/20260901120000/https://claude.com/pricing")
        self.assertIn("2 distinct captures", wayback.render_snapshots_text("https://claude.com/pricing", rows))


class Registry(unittest.TestCase):
    def test_resolve_aliases(self):
        self.assertEqual(registry.resolve("OpenAI, Inc.")["key"], "openai")
        self.assertEqual(registry.board_for("Anthropic"), ("greenhouse", "anthropic"))
        self.assertEqual(registry.cik_for("Salesforce"), 1108524)
        self.assertEqual(registry.cik_for("AWS"), 1018724)
        self.assertIsNone(registry.resolve("Acme Rockets"))
        self.assertIsNone(registry.board_for("Mistral AI"))


class Spec(unittest.TestCase):
    def test_private_company_and_unknown_tool_answer_honestly(self):
        h, b = toolspec.run("sec_fact", {"company": "Anthropic", "concept": "revenue"})
        self.assertEqual(h, ""); self.assertIn("No SEC filer", b)
        h, b = toolspec.run("job_postings", {"company": "Mistral AI"})
        self.assertEqual(h, ""); self.assertIn("No public job board", b)
        self.assertIn("unknown tool", toolspec.run("nope", {})[1])
        self.assertIn("needs an http", toolspec.run("page_history", {"url": "claude.com/pricing"})[1])

    def test_sec_fact_header_and_body(self):
        with mock.patch.object(edgar, "_get_json", return_value=CONCEPT_JSON):
            h, b = toolspec.run("sec_fact", {"company": "Salesforce", "concept": "Revenues"})
        self.assertTrue(h.startswith("SOURCE url=https://data.sec.gov/api/xbrl/companyconcept/CIK0001108524/us-gaap/Revenues.json class=filing tier=primary as_of=2026-07-31"))
        self.assertIn("us-gaap:Revenues end=2026-07-31 value=11345000000", b)
        self.assertNotIn("SOURCE url=", b)                                  # the header never sits in the body

    def test_tool_failure_never_raises(self):
        with mock.patch.object(edgar, "_get_json", side_effect=RuntimeError("HTTP 403")):
            h, b = toolspec.run("sec_filings", {"company": "HubSpot"})
        self.assertEqual(h, ""); self.assertIn("unreachable", b); self.assertIn("Do not guess", b)


class Adapters(unittest.TestCase):
    def test_sdk_and_loop_expose_the_same_tools(self):
        loop = {t["function"]["name"]: t["function"]["parameters"] for t in localagent.TOOLS if t["function"]["name"] in toolspec.NAMES}
        self.assertEqual(set(loop), set(toolspec.NAMES))
        self.assertEqual([n.split("__")[-1] for n in sources_tool.SOURCES_TOOL_NAMES], list(toolspec.NAMES))
        for spec in toolspec.TOOLS:
            self.assertEqual(set(loop[spec.name]["properties"]), set(spec.params))
            self.assertEqual(loop[spec.name]["required"], list(spec.required))
        self.assertEqual(sources_tool.TRIAGE_TOOL_NAMES, ["mcp__scoutsources__job_postings"])

    def test_loop_dispatch_carries_header_and_host(self):
        with mock.patch.object(toolspec, "run", return_value=("SOURCE url=https://data.sec.gov/api/x class=filing tier=primary as_of=2026-07-31", "BODY")):
            text, meta, ok = localagent._run_tool("sec_fact", {"company": "Salesforce", "concept": "revenue"})
        self.assertTrue(ok); self.assertTrue(text.startswith("[SOURCE url=")); self.assertIn("BODY", text)
        self.assertEqual(meta["hosts"], ["data.sec.gov"]); self.assertEqual(meta["status"], "ok")
        _, meta, ok = localagent._run_tool("sec_fact", {"company": "Salesforce"})
        self.assertFalse(ok); self.assertEqual(meta["status"], "bad_args")

    def test_flag_gates_everything(self):
        with mock.patch.object(config, "SOURCES_TOOLS_ENABLED", False):
            self.assertEqual((sources_tool.servers(), sources_tool.names(), sources_tool.note()), ({}, [], ""))
        with mock.patch.object(config, "SOURCES_TOOLS_ENABLED", True):
            self.assertEqual(list(sources_tool.servers()), ["scoutsources"])
            self.assertEqual(sources_tool.names("triage"), ["mcp__scoutsources__job_postings"])
            self.assertEqual(len(sources_tool.names()), 4)
            self.assertIn("NEVER paraphrase a filing number", sources_tool.note())
            self.assertIn("job_postings", sources_tool.note("triage"))


class XbrlGrounding(unittest.TestCase):
    URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK0001108524/us-gaap/Revenues.json"

    def _claim(self, excerpt):
        return {"id": "c_0123456789ab", "subject_key": "k", "claim": "x", "claim_type": "fact", "section": "recent_moves",
                "zone": None, "order": 0, "verified": True, "confidence": "high", "source_url": self.URL,
                "source_tier": "primary", "evidence_excerpt": excerpt, "as_of": "2026-07-31"}

    def test_exact_line_grounds_everything_else_is_absent(self):
        good = edgar.canonical_line("us-gaap", "Revenues", "USD", CONCEPT_JSON["units"]["USD"][0])
        cases = {good: "grounded", good.replace("11345000000", "11345000001"): "absent",
                 good.replace("end=2026-07-31", "end=2026-07-30"): "absent",
                 "Salesforce reported revenue of $11.3 billion for the quarter ended July 31, 2026, per its 10-Q.": "absent"}
        for ex, want in cases.items():
            with mock.patch.object(grounding, "_fetch_response", return_value=_Resp(CONCEPT_JSON)):
                g = grounding.ground_claims([self._claim(ex)])
            self.assertEqual(g["counts"][want], 1, ex)
            if want == "grounded":
                self.assertEqual(g["kept"][0]["grounding"]["method"], "substring")
                self.assertEqual(g["kept"][0]["grounding"]["fetched_via"], "xbrl")
                self.assertEqual(schema.validation_errors(g["kept"][0]), [])
            else:
                self.assertIn("xbrl", g["results"][0]["detail"])

    def test_live_pages_keep_the_fuzzy_path(self):
        class _Html:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = "https://www.cnbc.com/x"
            content = b""
            text = "<html><body><p>Salesforce reported revenue of $11.3 billion for the quarter ended July 31, 2026.</p></body></html>"
        c = self._claim("Salesforce reported revenue of $11.3 billion for the quarter ended July 31 2026")
        c["source_url"] = "https://www.cnbc.com/x"
        with mock.patch.object(grounding, "_fetch_response", return_value=_Html()):
            g = grounding.ground_claims([c])
        self.assertEqual(g["counts"]["grounded"], 1)
        self.assertNotIn("fetched_via", g["kept"][0]["grounding"])


class Purity(unittest.TestCase):
    def test_grounding_imports_no_model_sdk(self):
        """The provenance argument (grounding.py docstring) says CI asserts this; now it does."""
        src = open(os.path.join(os.path.dirname(grounding.__file__), "grounding.py")).read()
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertFalse({"anthropic", "claude_agent_sdk"} & names, names)


class AnchorRank(unittest.TestCase):
    def test_class_breaks_ties_within_a_tier(self):
        self.assertLess(monitor._source_rank("https://www.sec.gov/x", "primary"), monitor._source_rank("https://openai.com/x", "primary"))
        self.assertLess(monitor._source_rank("https://www.cnbc.com/x", "reputable_secondary"), monitor._source_rank("https://marc0.dev/x", "reputable_secondary"))
        self.assertLess(monitor._source_rank("https://marc0.dev/x", "reputable_secondary"), monitor._source_rank("https://news.ycombinator.com/x", "sentiment_only"))
        c = {"claim_type": "fact", "candidate_sources": [
            {"source_url": "https://www.cnbc.com/a", "source_tier": "reputable_secondary", "evidence_excerpt": "e" * 40},
            {"source_url": "https://www.sec.gov/Archives/x", "source_tier": "primary", "evidence_excerpt": "e" * 40},
            {"source_url": "https://openai.com/x", "source_tier": "primary", "evidence_excerpt": "e" * 40}]}
        self.assertEqual([v["source_url"] for v in monitor._candidate_variants(c)],
                         ["https://www.sec.gov/Archives/x", "https://openai.com/x", "https://www.cnbc.com/a"])


if __name__ == "__main__":
    unittest.main()
