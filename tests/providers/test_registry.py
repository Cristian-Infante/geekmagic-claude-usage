"""The providers are interchangeable to the rest of the app: each bundles how to read, count, watch and sign in."""
import unittest
from unittest.mock import patch

from geekmagic.insights import agent_activity, usage_stats
from geekmagic.providers import ALL, KEY_OF, PROVIDERS, TITLES, Provider, claude, codex
from geekmagic.render import animations, mascots


class RegistryTests(unittest.TestCase):
    def test_every_provider_is_registered_under_its_own_key_and_title(self):
        self.assertEqual([p.key for p in ALL], ["claude", "codex"], "in the order they cycle")
        self.assertEqual(PROVIDERS, {p.key: p for p in ALL})
        self.assertEqual(TITLES, {"claude": "Claude", "codex": "Codex"})
        self.assertEqual(KEY_OF, {"Claude": "claude", "Codex": "codex"}, "a reading says who it is by title")

    def test_each_one_says_how_to_sign_in_and_where_to_get_its_cli(self):
        for provider in ALL:
            self.assertTrue(provider.login_args, provider.key)
            self.assertTrue(provider.install_hint, provider.key)

    def test_each_one_has_a_look_and_animations_on_the_screen(self):
        for provider in ALL:
            self.assertIn(provider.title, mascots.THEMES)
            self.assertIn(provider.title, animations.ANIMATION_GROUPS)
            self.assertIn(animations.WORKING_ANIMATION[provider.title], animations.ANIMATION_GROUPS[provider.title])
            self.assertIn(animations.WAITING_ANIMATION[provider.title], animations.ANIMATION_GROUPS[provider.title])

    def test_it_is_an_interface_nobody_can_use_half_done(self):
        with self.assertRaises(TypeError):
            Provider()

        class Incomplete(Provider):
            key, title = "x", "X"

        with self.assertRaises(TypeError):
            Incomplete()

    def test_reading_counting_and_watching_go_to_each_providers_own_code(self):
        with patch.object(claude, "fetch_usage", return_value={"title": "Claude"}) as read:
            self.assertEqual(PROVIDERS["claude"].fetch(), {"title": "Claude"})
        read.assert_called_once()
        with patch.object(codex, "fetch_codex_usage", return_value={"title": "Codex"}):
            self.assertEqual(PROVIDERS["codex"].fetch(), {"title": "Codex"})
        with patch.object(usage_stats, "claude_stats", return_value={"total": 1}) as count:
            self.assertEqual(PROVIDERS["claude"].local_stats(5.0), {"total": 1})
        count.assert_called_once_with(5.0)
        with patch.object(usage_stats, "codex_stats", return_value=None):
            self.assertIsNone(PROVIDERS["codex"].local_stats(5.0))
        with patch.object(agent_activity, "codex_state", return_value={"working": True}) as watch:
            self.assertEqual(PROVIDERS["codex"].agent_state(7.0), {"working": True})
        watch.assert_called_once_with(7.0)
        with patch.object(agent_activity, "claude_state", return_value={"working": False}):
            self.assertEqual(PROVIDERS["claude"].agent_state(7.0), {"working": False})

    def test_each_one_finds_its_own_cli(self):
        with patch.object(claude, "which", return_value="/bin/claude") as which:
            self.assertEqual(PROVIDERS["claude"].find_cli(), "/bin/claude")
        which.assert_called_once_with("claude")
        with patch.object(codex, "find_codex", return_value=None):
            self.assertIsNone(PROVIDERS["codex"].find_cli())


if __name__ == "__main__":
    unittest.main()
