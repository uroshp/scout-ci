"""scout/mcp_server.py + the engine's /mcp mount (WS4, 2026-09-29): tool shapes, real roster reads,
ask_scout faked (no spend) through the ledger, and the HTTP gate (reads free, ask_scout owner-only)."""
import asyncio
import json
import os
import unittest
from unittest import mock

from scout import config, display, ledger, selfserve
from scout import mcp_server as m


def _call(name, args):
    r = asyncio.run(m.mcp.call_tool(name, args))
    content = r[0] if isinstance(r, tuple) else r
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    try:
        return [json.loads(t) for t in texts] if len(texts) != 1 else json.loads(texts[0])
    except Exception:
        return texts[0] if len(texts) == 1 else texts


class Tools(unittest.TestCase):
    def test_shapes_and_reads(self):
        names = [t.name for t in asyncio.run(m.mcp.list_tools())]
        self.assertEqual(names, ["list_battlecards", "get_battlecard", "recent_changes", "sources", "ask_scout"])
        cards = _call("list_battlecards", {})
        cards = cards if isinstance(cards, list) else [cards]
        self.assertTrue(cards and all({"slug", "card", "last_checked", "claims", "url"} <= set(c) for c in cards))
        slug = display.list_battlecards()[0]
        md = _call("get_battlecard", {"slug": slug})
        self.assertIn("Verified by Scout", md); self.assertIn(f"/c/{slug}", md)
        sec = _call("get_battlecard", {"slug": slug, "section": "pricing", "persona": "economic_buyer"})
        self.assertLess(len(sec), len(md))
        with self.assertRaises(Exception):
            _call("get_battlecard", {"slug": "../etc/passwd"})
        with self.assertRaises(Exception):
            _call("get_battlecard", {"slug": slug, "section": "nope"})
        src = _call("sources", {"slug": slug})
        src = src if isinstance(src, list) else [src]
        self.assertTrue(src and all({"url", "class", "tier", "claims"} <= set(s) for s in src))
        rc = _call("recent_changes", {"slug": slug, "days": 30})
        self.assertIsInstance(rc, (list, dict))

    def test_ask_scout_goes_through_the_ledger_and_is_faked_here(self):
        files = {}
        fake = {"id": "a_0123456789ab", "kind": "quick", "card": "X vs Y", "competitor": "Y", "paragraphs": [{"text": "Y grew.", "cites": [1]}],
                "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary", "as_of": "2026-09-01", "excerpt": "Y grew 10%"}],
                "cut_log": [], "unanswered": [], "verified": True, "seconds": 20.0, "cost_usd": 0.5}
        with mock.patch.object(selfserve, "read_data", side_effect=files.get), \
             mock.patch.object(selfserve, "update_data", side_effect=lambda p, tx, msg, **kw: files.__setitem__(p, tx(files.get(p))) or True), \
             mock.patch("scout.ask.quick_ask", return_value=fake) as q, mock.patch("scout.ask.ask") as deep:
            out = _call("ask_scout", {"question": "Did Y grow?"})
        q.assert_called_once(); deep.assert_not_called()
        self.assertEqual(out["kind"], "quick"); self.assertEqual(out["permalink"], "https://agent-scout.ai/answers/a_0123456789ab")
        self.assertEqual(json.loads(files["ask/state.json"])["spend_usd"], 0.5)          # settled at the real cost
        with mock.patch.object(selfserve, "read_data", side_effect=files.get), \
             mock.patch.object(selfserve, "update_data", side_effect=lambda p, tx, msg, **kw: files.__setitem__(p, tx(files.get(p))) or True), \
             mock.patch.dict(os.environ, {"SCOUT_ASK_DAILY_CEILING_USD": "1"}), mock.patch("scout.ask.quick_ask", return_value=fake):
            with self.assertRaises(Exception):
                _call("ask_scout", {"question": "again?"})                                  # 0.5 + 1.5 > 1: refused before any spend


class HttpGate(unittest.TestCase):
    def test_reads_pass_and_ask_needs_an_owner_key(self):
        from fastapi.testclient import TestClient
        import importlib
        with mock.patch.dict(os.environ, {"SCOUT_MCP": "1", "ASK_API_KEYS": "owner-key"}):
            from engine import app as eng
            eng = importlib.reload(eng)
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
            h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            with TestClient(eng.app) as c:                                                                # lifespan: the session manager runs
                r = c.post("/mcp", json=init, headers=h)
                self.assertEqual(r.status_code, 200, r.text[:200])                                       # stateless: no session id needed
                call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ask_scout", "arguments": {"question": "q"}}}
                with mock.patch("scout.mcp_server._run_ask") as run:
                    r = c.post("/mcp", json=call, headers=h)
                    self.assertEqual(r.status_code, 200); self.assertIn("owner", r.text); self.assertIn("isError", r.text)   # metered: no key -> tool error, no run
                    r = c.post("/mcp", json=call, headers={**h, "Authorization": "Bearer nope"})
                    self.assertIn("owner", r.text); run.assert_not_called()
                    run.return_value = {"id": "a_0123456789ab", "kind": "quick", "card": "X vs Y", "paragraphs": [], "sources": [], "cut_log": [], "unanswered": [], "verified": False, "seconds": 1, "cost_usd": 0.1}
                    r = c.post("/mcp", json=call, headers={**h, "Authorization": "Bearer owner-key"})
                    self.assertEqual(r.status_code, 200); self.assertIn("a_0123456789ab", r.text); run.assert_called_once()   # the owner's key runs it
                read = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_battlecards", "arguments": {}}}
                r = c.post("/mcp", json=read, headers=h)
                self.assertEqual(r.status_code, 200); self.assertIn("agent-scout.ai/c/", r.text)             # a read is free
                lst = c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, headers=h)
                self.assertIn("ask_scout", lst.text)
        with mock.patch.dict(os.environ, {"SCOUT_MCP": "0"}):
            eng2 = importlib.reload(eng)
            with TestClient(eng2.app) as c2:
                self.assertEqual(c2.post("/mcp", json=init, headers=h).status_code, 404)                   # off = not mounted
        importlib.reload(eng)
