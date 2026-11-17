from unittest import TestCase

from app import config
from app.core.config import search


class SearchConfigTest(TestCase):
    def test_legacy_facade_matches_domain(self):
        self.assertEqual(config.SEARCH_API_KEY, search.SEARCH_API_KEY)
        self.assertEqual(config.DOUBAO_SEARCH_ENDPOINT,
                         search.DOUBAO_SEARCH_ENDPOINT)
        self.assertEqual(config.WEB_SEARCH_MAX_CHARS,
                         search.WEB_SEARCH_MAX_CHARS)

    def test_truncation_limit_is_positive(self):
        self.assertGreater(search.WEB_SEARCH_MAX_CHARS, 0)

    def test_endpoint_is_http(self):
        self.assertTrue(search.DOUBAO_SEARCH_ENDPOINT.startswith("http"))
