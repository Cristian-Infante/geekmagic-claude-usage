"""A provider that is signed out gets its sign-in opened when you pick it, and its screen says what to do."""
import time
import unittest
from unittest.mock import patch

try:
    from tests import test_tray_logic
    import tray
except unittest.SkipTest:
    raise
except Exception as e:  # pystray needs a desktop session
    raise unittest.SkipTest(f"tray can't be imported here: {e}")

import geekmagic_claude as g

usage = test_tray_logic.usage


def failing(error):
    def read():
        raise error
    return read


SIGNED_OUT = {"codex": failing(g.SignInNeeded("Codex could not read account limits. Not signed in?"))}


class TraySignInTests(unittest.TestCase):
    def setUp(self):
        # the harness of the tray tests (temp state file, no real device, nothing sent): borrowed, not subclassed
        self.base = test_tray_logic.TrayLogicTests("test_notifications_can_be_switched_off_but_state_keeps_tracking")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.opened, self.outcome = [], "started"
        patcher = patch.object(tray.login, "launch", side_effect=lambda p: self.opened.append(p) or self.outcome)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self):
        app = self.base.make()
        app._run_in_background = lambda func, *args: func(*args)  # no threads: what happens is what's asserted
        return app

    def sign_out(self, app, provider="codex"):
        with patch.dict(g.PROVIDERS, {provider: failing(g.SignInNeeded(f"{provider} is not signed in."))}):
            app._fetch_usage(provider)

    def test_picking_a_signed_out_provider_opens_its_sign_in_once(self):
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
            self.assertEqual(self.opened, ["codex"])
            self.assertIn("window opened", app.errors["codex"])
            self.assertIn(("Codex", "Inicia sesión en la ventana que se abrió"), self.base.notes)
            app.select("claude")
            app.select("codex")
            self.assertEqual(self.opened, ["codex"], "not again within a few minutes: cycling past it opens nothing new")
            app.login_at["codex"] -= tray.LOGIN_COOLDOWN + 1
            app.select("codex")
            self.assertEqual(self.opened, ["codex", "codex"], "...but it comes back if it still isn't signed in")

    def test_a_provider_that_is_signed_in_opens_nothing(self):
        app = self.make()
        with patch.object(g, "show_image"):
            app.select("codex")
            app.select("claude")
        self.assertEqual(self.opened, [])

    def test_it_is_for_every_provider(self):
        app = self.make()
        readers = {"claude": failing(g.SignInNeeded("Claude Code isn't signed in.")), "codex": failing(g.UsageError("timed out"))}
        with patch.dict(g.PROVIDERS, readers):
            app._fetch_usage("claude")
            app._fetch_usage("codex")
        self.assertEqual(app.needs_login, {"claude"}, "a timeout isn't a reason to sign in again")
        with patch.object(g, "show_image"):
            app.select("codex")
            app.select("claude")
        self.assertEqual(self.opened, ["claude"])

    def test_a_missing_cli_says_how_to_install_it_instead(self):
        self.outcome = "missing"
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
        self.assertIn("Install the Codex CLI", app.errors["codex"])
        self.assertFalse(app._error_usage("codex")["signin"], "there is nothing to sign in to yet")

    def test_no_terminal_still_leaves_a_message(self):
        self.outcome = "failed"
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
        self.assertIn("Couldn't open a terminal", app.errors["codex"])

    def test_while_the_window_is_open_a_failed_read_keeps_the_instructions(self):
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
        note = app.errors["codex"]
        with patch.dict(g.PROVIDERS, SIGNED_OUT):
            app._fetch_usage("codex")
        self.assertEqual(app.errors["codex"], note)
        self.assertTrue(app._login_pending("codex"))
        app.login_at["codex"] -= tray.LOGIN_COOLDOWN + 1
        with patch.dict(g.PROVIDERS, SIGNED_OUT):
            app._fetch_usage("codex")
        self.assertIn("Not signed in?", app.errors["codex"], "after that, the plain reason")

    def test_once_signed_in_the_next_read_clears_everything(self):
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
        with patch.dict(g.PROVIDERS, {"codex": lambda: usage(title="Codex")}):
            self.assertIsNotNone(app._fetch_usage("codex"))
        self.assertEqual((app.errors, app.needs_login, app.login_notes, app._login_pending("codex")), ({}, set(), {}, False))

    def test_the_screen_gets_the_explanation_once_and_not_again_every_cycle(self):
        app = self.make()
        self.sign_out(app)
        with patch.object(g, "show_image"):
            app.select("codex")
        shown = app._error_usage("codex")
        self.assertTrue(shown["signin"])
        self.assertEqual(shown["error"], app.errors["codex"])
        sent = []
        with patch.object(g, "push_error", side_effect=lambda ip, u, name, **k: sent.append((u["title"], name))), \
                patch.object(g, "show_image"):
            app.last_click = 0
            self.assertTrue(app._upload("codex", shown))
        self.assertEqual([title for title, _ in sent], ["Codex"])
        self.assertIsNone(app._error_usage("codex"), "it already says it")
        app.errors["codex"] = "something else now"
        self.assertIsNotNone(app._error_usage("codex"), "a different reason is worth saying")

    def test_a_provider_that_was_read_before_is_dimmed_not_replaced_by_an_error_screen(self):
        app = self.make()
        app.last_good["claude"] = (usage(), time.monotonic())
        app.errors["claude"] = "timed out"
        self.assertIsNone(app._error_usage("claude"))

    def test_the_menu_can_always_open_a_sign_in(self):
        app = self.make()
        options = next(i for i in app.icon.menu.items if i.text == "Opciones")
        item = next(i for i in options.submenu.items if i.text == "Iniciar sesión")
        self.assertEqual([i.text for i in item.submenu.items], ["Claude", "Codex"])
        app._login_action("claude")()  # signed in, as far as it knows: it's still what was asked for
        deadline = time.time() + 2
        while not self.opened and time.time() < deadline:  # (the menu runs it in a thread)
            time.sleep(0.01)
        self.assertEqual(self.opened, ["claude"])

    def test_it_looks_again_often_while_a_sign_in_is_pending(self):
        app = self.make()
        self.sign_out(app)
        self.assertFalse(app._login_pending("codex"))
        with patch.object(g, "show_image"):
            app.select("codex")
        self.assertTrue(app._login_pending("codex"))
        self.assertLess(tray.LOGIN_POLL, app.interval)

    def test_a_split_panel_for_a_provider_that_cant_be_read_keeps_its_place(self):
        app = self.make()
        app.last_good["claude"] = (usage(), time.monotonic())
        app.errors["codex"] = "Codex is not signed in."
        panels = app._panels(tray.SPLIT)
        self.assertEqual([p["title"] for p in panels], ["Claude", "Codex"])
        self.assertIsNone(panels[1]["current_pct"])
        app.last_good.clear()
        self.assertEqual(app._panels(tray.SPLIT), [], "nothing readable at all: nothing to draw")


if __name__ == "__main__":
    unittest.main()
