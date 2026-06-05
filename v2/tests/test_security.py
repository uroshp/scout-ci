"""Security-relevant invariants: input sanitization, id validation, SSRF guard.

These guard the untrusted-input boundary of the self-serve flow + the grounding fetcher.
Run from v2/:  python -m unittest discover -s tests
"""
import unittest

from scout import store, selfserve
from scout.grounding import _assert_fetchable, BlockedURLError


class SlugSanitization(unittest.TestCase):
    """make_slug/_slug_part build file + GitHub-API paths from user input, so they MUST NOT
    let path-traversal or separators through (a card folder is named by these)."""

    def test_traversal_is_neutralized(self):
        for hostile in ["../../etc/passwd", "..\\..\\win", "a/b/c", "foo..bar", "  ../x  "]:
            slug = store.make_slug(hostile)
            self.assertNotIn("/", slug)
            self.assertNotIn("\\", slug)
            self.assertNotIn("..", slug)

    def test_charset_is_slug_safe(self):
        slug = store.make_slug("OpenAI!!!", "Anthropic & Co", "enterprise/coding")
        self.assertRegex(slug, r"^[a-z0-9_-]+$")

    def test_shape_is_perspective_encoded(self):
        self.assertEqual(
            store.make_slug("OpenAI", "Anthropic", "enterprise coding"),
            "anthropic__vs__openai__enterprise-coding",
        )
        self.assertEqual(store.make_slug("OpenAI"), "scout__vs__openai__general")


class JobIdValidation(unittest.TestCase):
    """valid_job_id guards a URL-supplied id before it is used in a filesystem/API path."""

    def test_accepts_real_ids(self):
        self.assertTrue(selfserve.valid_job_id("anthropic__vs__openai__general__20260605-120000"))

    def test_rejects_traversal_and_junk(self):
        for bad in ["../etc", "a/../b", "..", "ab", "", "Foo__Bar", "x" * 200, "a b", "a;b"]:
            self.assertFalse(selfserve.valid_job_id(bad), bad)


class SSRFGuard(unittest.TestCase):
    """The fetcher must refuse non-public hosts + non-http schemes (the URL is model-chosen)."""

    def test_blocks_internal_and_bad_schemes(self):
        for url in [
            "http://169.254.169.254/latest/meta-data/",   # cloud metadata (link-local)
            "http://localhost/", "http://127.0.0.1/",      # loopback
            "http://10.0.0.1/", "http://192.168.1.1/",     # private
            "file:///etc/passwd", "ftp://host/x", "gopher://host/",
        ]:
            with self.assertRaises(BlockedURLError, msg=url):
                _assert_fetchable(url)

    def test_allows_public_https(self):
        _assert_fetchable("https://www.sec.gov/cgi-bin/browse-edgar")  # must not raise


if __name__ == "__main__":
    unittest.main()
