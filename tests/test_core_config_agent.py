from unittest import TestCase

from app import config
from app.core.config import agent


class AgentConfigTest(TestCase):
    def test_legacy_facade_matches_domain(self):
        self.assertEqual(config.DEFAULT_AGENT_ID, agent.DEFAULT_AGENT_ID)

    def test_default_agent_is_non_empty(self):
        self.assertTrue(agent.DEFAULT_AGENT_ID.strip())
