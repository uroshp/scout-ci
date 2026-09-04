"""The Streamlit retirement stub (app_v2.py, 2026-09-04) must always render the escape path:
a link to agent-scout.ai. If this ever fails, June-resume clicks hit a dead end."""
import unittest


class StubRenders(unittest.TestCase):
    def test_stub_boots_and_offers_the_new_home_link(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file("../app_v2.py", default_timeout=30)
        at.run()
        self.assertFalse(at.exception, f"stub crashed: {at.exception}")
        text = " ".join(str(getattr(el, "value", "")) for el in at.markdown) + \
               " ".join(t.value for t in at.title)
        self.assertIn("agent-scout.ai", text)
        self.assertIn("Scout has moved", text)
