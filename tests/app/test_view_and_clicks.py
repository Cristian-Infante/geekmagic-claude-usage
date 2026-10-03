"""Clicking and the menu: which view is on screen, the split view being menu-only, and what the device remembers of it."""
import unittest
from unittest.mock import patch

from geekmagic.app.viewstate import SPLIT
from geekmagic.device.client import GeekMagicDevice
from tests.support import AppTestCase, capture_screen


class SplitViewClicksTests(AppTestCase):
    def test_split_view_is_only_turned_on_from_the_menu_and_left_click_leaves_it(self):
        app = self.make()
        with capture_screen():
            self.assertEqual(app.view.key, "claude")
            app.toggle()  # the click only ever alternates Claude <-> Codex
            self.assertEqual((app.view.split, app.view.key), (False, "codex"))
            app.toggle_mode(SPLIT)
            self.assertEqual((app.view.split, app.view.key), (True, "split"))
            self.assertIn("dividida", app._tooltip())
            app.toggle()  # from the split view the click goes back to where you were
            self.assertEqual((app.view.split, app.view.key), (False, "codex"))
            app.toggle_mode(SPLIT)
            app.toggle_mode(SPLIT)  # the menu item toggles it off again
            self.assertEqual((app.view.split, app.view.key), (False, "codex"))
            app.toggle_mode(SPLIT)
            app.select("claude")  # picking a provider in the menu leaves it too
            self.assertEqual((app.view.split, app.view.key), (False, "claude"))

    def test_menu_has_the_split_item_next_to_the_other_buttons(self):
        app = self.make()
        names = [str(item) for item in app.icon.menu.items]
        self.assertIn("Vista dividida", names)
        self.assertLess(names.index("Codex"), names.index("Vista dividida"))
        self.assertLess(names.index("Vista dividida"), names.index("Ver logs"))

    def test_instant_switch_into_split_shows_its_last_image_only_while_active(self):
        app = self.make()
        app.screen.slots["split"] = "b"
        with capture_screen() as cap:
            app.screen.show_uploaded("split")  # not in split view: nothing happens
            self.assertEqual(cap.shown, [])
            app.view.split = True
            app.screen.show_uploaded("split")
        self.assertEqual(cap.shown, ["split-usage-b.gif"])

    def test_split_images_are_remembered_across_restarts_and_synced_with_the_device(self):
        app = self.make()
        app.screen.slots["split"] = "a"
        app.persistence.save()
        self.assertEqual(self.make().screen.slots.get("split"), "a")
        with patch.object(GeekMagicDevice, "list_images", return_value=set()):  # the device lost its files
            again = self.make()
            again.screen.sync_with_device()
        self.assertNotIn("split", again.screen.slots)


if __name__ == "__main__":
    unittest.main()
