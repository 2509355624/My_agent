"""角色卡配置域：内置卡保护名单与旧 app.config 同值。"""

import unittest

from app import config
from app.core.config import card, web


class CardConfigTest(unittest.TestCase):
    def test_old_and_new_paths_expose_the_same_set(self):
        self.assertTrue(hasattr(card, "BUILTIN_CARD_IDS"))
        self.assertEqual(config.BUILTIN_CARD_IDS, card.BUILTIN_CARD_IDS)
        self.assertEqual(card.BUILTIN_CARD_IDS, frozenset(card.BUILTIN_CARD_IDS))

    def test_known_cards_are_protected(self):
        self.assertIn("rem", card.BUILTIN_CARD_IDS)
        self.assertIn("izumi_sagiri", card.BUILTIN_CARD_IDS)

    def test_card_list_is_not_in_web_domain(self):
        self.assertFalse(hasattr(web, "BUILTIN_CARD_IDS"))
        self.assertIn("BUILTIN_CARD_IDS", card.__all__)


if __name__ == "__main__":
    unittest.main()
