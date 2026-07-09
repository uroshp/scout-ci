"""Analytics host-guard invariants (the 2026-07-08 incident): the client GA tag's hostname gate
is pinned to BOTH prod surfaces by its OWN config (ANALYTICS_HOSTNAMES) and can never again be
silently disarmed by repointing the link-base SELFSERVE_APP_URL. Pure, no model/network.

    python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from scout import analytics, config

PROD_HOSTS = ("agent-scout.ai", "agent-scout.streamlit.app")


class HostGuardPinned(unittest.TestCase):
    def test_component_guard_lists_both_prod_hosts(self):
        html = analytics.ga_component_html("G-TEST")
        for host in PROD_HOSTS:
            self.assertIn(host, html)

    def test_selfserve_url_repoint_cannot_disarm_the_guard(self):
        # THE regression: the 7/7 URL swap moved SELFSERVE_APP_URL and (latently) killed the tag
        # on the mirror. The guard must now be immune to that setting entirely.
        with mock.patch.object(config, "SELFSERVE_APP_URL", "https://somewhere-else.example"):
            html = analytics.ga_component_html("G-TEST")
        for host in PROD_HOSTS:
            self.assertIn(host, html)
        self.assertNotIn("somewhere-else.example", html)

    def test_server_head_guard_lists_both_prod_hosts(self):
        import server
        with mock.patch.object(config, "GA_MEASUREMENT_ID", "G-TEST"):
            head = server._ga_head()
        for host in PROD_HOSTS:
            self.assertIn(host, head)

    def test_both_injectors_share_one_allowlist(self):
        # The guard must come from the same helper on both surfaces — no drift.
        import server
        js = analytics._hosts_js()
        with mock.patch.object(config, "GA_MEASUREMENT_ID", "G-TEST"):
            self.assertIn(js, server._ga_head())
        self.assertIn(js, analytics.ga_component_html("G-TEST"))

    def test_empty_allowlist_skips_guard_rather_than_blocking(self):
        # Misconfiguration fallback: an empty list must not silently kill GA everywhere —
        # same semantics as the old unset-URL fallback.
        with mock.patch.object(config, "ANALYTICS_HOSTNAMES", ()):
            html = analytics.ga_component_html("G-TEST")
        self.assertNotIn("indexOf(p.location.hostname)", html)
        self.assertIn("googletagmanager.com", html)


if __name__ == "__main__":
    unittest.main()
