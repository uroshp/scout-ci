"""SEC EDGAR client (WS1, 2026-09-28). Model-free, httpx only, through grounding's SSRF-guarded
fetch with the SEC fair-access User-Agent.

Two endpoints, both keyless:
  submissions   data.sec.gov/submissions/CIK##########.json      -> the filings index (8-K, 10-Q, 10-K, D ...)
  companyconcept data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/<Concept>.json
                -> every reported value of one XBRL concept, with period, form, fiscal year/period

The design point (plan C48): an EDGAR FACT IS A NUMBER CODE CAN VERIFY, not a text excerpt. The
tool hands the model a canonical one-line rendering of the fact (`canonical_line`), the model cites
the companyconcept URL and copies that line as its evidence_excerpt, and grounding re-fetches the
JSON and renders EVERY fact the same way (`canonical_lines`), so the ordinary substring check
proves the number, period and form are exactly what the SEC reported. No page snapshot, no fuzzy
match on prose, and nothing a model can paraphrase.
"""
from __future__ import annotations

import json
import re
from datetime import date

SEC_API = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"
DEFAULT_FORMS = ("8-K", "10-Q", "10-K", "10-K/A", "10-Q/A", "D", "D/A", "S-1", "424B4")
# The concepts a CI reader asks about, by plain name. Extend as cards need.
CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "net income": ["NetIncomeLoss"],
    "operating income": ["OperatingIncomeLoss"],
    "rpo": ["RevenueRemainingPerformanceObligation"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "r&d": ["ResearchAndDevelopmentExpense"],
    "sales and marketing": ["SellingAndMarketingExpense"],
    "employees": ["EntityNumberOfEmployees"],
    "shares": ["EntityCommonStockSharesOutstanding"],
}
_XBRL_RE = re.compile(r"^https?://data\.sec\.gov/api/xbrl/(companyconcept|companyfacts)/", re.I)


def is_xbrl_url(url) -> bool:
    return bool(_XBRL_RE.match(str(url or "")))


def _cik10(cik) -> str:
    return f"{int(cik):010d}"


def _get_json(url: str):
    from scout.grounding import _fetch_response
    resp = _fetch_response(url)
    if resp is None or resp.status_code >= 400:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} for {url}")
    return resp.json()


# --- filings index --------------------------------------------------------------------------------
def filings(cik, forms=DEFAULT_FORMS, since: str | None = None, limit: int = 20) -> list[dict]:
    """Recent filings for a CIK, newest first: {form, filed, accession, primary_doc, url, description}."""
    data = _get_json(f"{SEC_API}/submissions/CIK{_cik10(cik)}.json")
    rec = data.get("filings", {}).get("recent", {})
    rows = []
    for i, form in enumerate(rec.get("form", [])):
        if forms and form not in forms:
            continue
        filed = rec.get("filingDate", [""])[i]
        if since and filed < since:
            continue
        accn = rec.get("accessionNumber", [""])[i]
        doc = rec.get("primaryDocument", [""])[i]
        acc_nodash = accn.replace("-", "")
        rows.append({"form": form, "filed": filed, "accession": accn, "primary_doc": doc,
                     "url": f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}",
                     "index_url": f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_nodash}/",
                     "description": rec.get("primaryDocDescription", [""])[i] or "",
                     "items": rec.get("items", [""])[i] or ""})
        if len(rows) >= limit:
            break
    return rows


def company_name(cik) -> str | None:
    try:
        return _get_json(f"{SEC_API}/submissions/CIK{_cik10(cik)}.json").get("name")
    except Exception:
        return None


# --- XBRL facts -----------------------------------------------------------------------------------
def concept_url(cik, concept: str, taxonomy: str = "us-gaap") -> str:
    return f"{SEC_API}/api/xbrl/companyconcept/CIK{_cik10(cik)}/{taxonomy}/{concept}.json"


def canonical_line(taxonomy: str, concept: str, unit: str, f: dict) -> str:
    """ONE line per reported value. The same function renders the tool's answer and grounding's
    page text, so the model's evidence_excerpt is a substring of the re-fetched rendering iff the
    number, period, unit, form and fiscal tag are exactly what the SEC has."""
    return (f"{taxonomy}:{concept} end={f.get('end')} value={f.get('val')} unit={unit} "
            f"form={f.get('form')} fy={f.get('fy')} fp={f.get('fp')} filed={f.get('filed')} accn={f.get('accn')}")


def canonical_lines(data: dict) -> list[str]:
    """Every fact in a companyconcept (or companyfacts) JSON, one canonical line each."""
    out = []
    if "units" in data:                                  # companyconcept
        tax, concept = data.get("taxonomy", "us-gaap"), data.get("tag", "")
        for unit, facts in (data.get("units") or {}).items():
            for f in facts:
                out.append(canonical_line(tax, concept, unit, f))
        return out
    for tax, concepts in (data.get("facts") or {}).items():   # companyfacts
        for concept, body in concepts.items():
            for unit, facts in (body.get("units") or {}).items():
                for f in facts:
                    out.append(canonical_line(tax, concept, unit, f))
    return out


def fact(cik, concept: str, taxonomy: str = "us-gaap", forms=("10-Q", "10-K"), limit: int = 8) -> dict:
    """The latest reported values of one concept, newest period first, de-duplicated to the latest
    filing per (end, fp, fy). Returns {url, concept, unit, rows:[{end, val, fy, fp, form, filed,
    accn, line}], name}. Raises on HTTP errors so the tool can say "unreachable" honestly."""
    url = concept_url(cik, concept, taxonomy)
    data = _get_json(url)
    best: dict = {}
    unit_used = None
    for unit, facts in (data.get("units") or {}).items():
        for f in facts:
            if forms and f.get("form") not in forms:
                continue
            key = (f.get("end"), f.get("fp"), f.get("fy"))
            if key not in best or str(f.get("filed", "")) > str(best[key][1].get("filed", "")):
                best[key] = (unit, f)
                unit_used = unit
    rows = sorted(best.values(), key=lambda uf: (str(uf[1].get("end", "")), str(uf[1].get("filed", ""))), reverse=True)[:limit]
    return {"url": url, "concept": f"{taxonomy}:{concept}", "unit": unit_used, "name": data.get("entityName"),
            "rows": [{**{k: f.get(k) for k in ("end", "val", "fy", "fp", "form", "filed", "accn", "start")},
                      "line": canonical_line(taxonomy, concept, unit, f)} for unit, f in rows]}


def resolve_concepts(plain: str) -> list[str]:
    """'revenue' -> the XBRL concept names to try, most common first; an exact concept name passes through."""
    p = str(plain or "").strip()
    if p in CONCEPTS:
        return CONCEPTS[p]
    low = p.lower()
    for k, v in CONCEPTS.items():
        if k == low:
            return v
    return [p] if p else []


def fact_any(cik, plain_or_concept: str, **kw) -> dict | None:
    """Try each concept name for a plain-English ask ('revenue') until one has rows."""
    for c in resolve_concepts(plain_or_concept):
        try:
            r = fact(cik, c, **kw)
        except Exception:
            continue
        if r["rows"]:
            return r
    return None


def render_fact_text(r: dict) -> str:
    """The tool's answer body: the canonical lines the model must copy verbatim as evidence."""
    lines = [f"{r.get('name') or 'Company'} · {r['concept']} · unit {r['unit']} · newest first",
             "Cite the URL above as source_url and copy ONE line below, character for character, as the evidence_excerpt:"]
    for row in r["rows"]:
        lines.append(row["line"])
    return "\n".join(lines)


def render_filings_text(rows: list[dict], name: str | None = None) -> str:
    if not rows:
        return "No filings matched."
    out = [f"{name or 'Company'} · {len(rows)} filings, newest first (form · filed · primary document URL)"]
    for r in rows:
        extra = f" · items {r['items']}" if r.get("items") else ""
        out.append(f"{r['form']} · {r['filed']} · {r['url']}{extra}")
    return "\n".join(out)


def as_of_today() -> str:
    return date.today().isoformat()
