"""The tray menu: its items and their order, and the night-mode settings that come from the command line."""
import argparse
import unittest
from unittest.mock import patch

from geekmagic.app import backlight, config, tray_app
from geekmagic.app.main import night_hours
from geekmagic.device.client import GeekMagicDevice
from tests.support import AppTestCase


class MenuTests(AppTestCase):
    def test_the_menu_has_every_new_button_in_a_sensible_order(self):
        app = self.make()
        top = [item.text for item in app.icon.menu.items if not item.text.startswith("-")]  # (no separators)
        for wanted in ("Vista dividida", "Estadísticas", "Más vistas", "Actualizar ahora", "Pausar", "Pantalla", "Opciones", "Ver logs", "Salir"):
            self.assertIn(wanted, top)
        self.assertLess(top.index("Vista dividida"), top.index("Estadísticas"))
        self.assertLess(top.index("Estadísticas"), top.index("Actualizar ahora"))
        self.assertLess(top.index("Opciones"), top.index("Ver logs"))
        sub = {i.text: [s.text for s in i.submenu.items if not s.text.startswith("-")]
               for i in app.icon.menu.items if i.submenu}
        self.assertEqual([x for x in sub["Pantalla"] if x.startswith("Brillo")],
                         [f"Brillo {level} %" for level in config.DEFAULT_BRIGHTNESS_CHOICES])
        self.assertIn("Atenuar al bloquear el PC", sub["Pantalla"])
        self.assertTrue(any(x.startswith("Modo nocturno") for x in sub["Pantalla"]))
        self.assertEqual(sub["Opciones"], ["Iniciar sesión", "Notificaciones", "Avisar cuando un agente termine o te espere", "Pausar al bloquear el PC"])
        self.assertEqual(sub["Más vistas"], ["Proyectos y modelos", "Horas pico"])

    def test_night_hours_argument(self):
        self.assertEqual(night_hours("22-7"), (22, 7))
        self.assertEqual(night_hours("0-23"), (0, 23))
        for bad in ("22", "24-7", "a-b", "22-7-9", "-3-5", ""):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                night_hours(bad)

    def test_night_settings_from_the_command_line_feed_the_menu_item(self):
        app = self.make(night_hours=(23, 6), night_level=5)
        night = app.backlight.night
        self.assertEqual((night["start"], night["end"], night["level"]), (23, 6, 5))
        self.assertFalse(night["enabled"], "defining the schedule doesn't switch it on")
        self.assertEqual(app.backlight.night_label(), "Modo nocturno (11 PM - 6 AM, brillo 5 %)")

    def test_brightness_menu_items_act_on_their_own_level(self):
        app = self.make()
        for module in (backlight, tray_app):  # run the work the app hands to a thread right away
            patcher = patch.object(module, "in_background", lambda f, *a: f(*a))
            patcher.start()
            self.addCleanup(patcher.stop)
        screen = next(i for i in app.icon.menu.items if i.text == "Pantalla")
        item = next(i for i in screen.submenu.items if i.text == "Brillo 25 %")
        with patch.object(GeekMagicDevice, "set_brightness", autospec=True) as send:
            item(app.icon)
        send.assert_called_once_with(app.screen.device, 25)
        self.assertTrue(item.checked)


if __name__ == "__main__":
    unittest.main()
