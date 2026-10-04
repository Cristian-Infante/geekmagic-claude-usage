"""A provider that is signed out gets its sign-in opened when you pick it, and its screen says what to do."""
import os
import time
from unittest.mock import patch

from geekmagic.app import config
from geekmagic.app import tray_app
from geekmagic.app.viewstate import SPLIT
from geekmagic.errors import SignInNeeded, UsageError
from geekmagic.providers import PROVIDERS
from geekmagic.system import login
from tests.support import AppTestCase, capture_screen, fake_fetchers, failing, usage

SIGNED_OUT = {"codex": failing(SignInNeeded("Codex could not read account limits. Not signed in?"))}


class SignInTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.opened, self.outcome = [], "started"
        # no test may open a real window: the launcher only records what it was asked to open
        # (it is handed the CLI's path: record whose it is) ...
        patcher = patch.object(login, "launch", side_effect=lambda argv, title: self.opened.append(os.path.basename(argv[0])) or self.outcome)
        patcher.start()
        self.addCleanup(patcher.stop)
        # ...and every provider's CLI is "installed" unless a test says otherwise
        for key, provider in PROVIDERS.items():
            patcher = patch.object(provider, "find_cli", return_value=f"/bin/{key}")
            patcher.start()
            self.addCleanup(patcher.stop)
        # picking a provider starts its sign-in in a thread: run it right here, so what happens is what's asserted
        patcher = patch.object(tray_app, "in_background", side_effect=lambda func, *args: func(*args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_out(self, app, provider="codex"):
        with fake_fetchers(**{provider: failing(SignInNeeded(f"{provider} is not signed in."))}):
            app.usage.fetch(provider)

    def test_picking_a_signed_out_provider_opens_its_sign_in_once(self):
        app = self.make()
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
            self.assertEqual(self.opened, ["codex"])
            self.assertIn("window opened", app.usage.errors["codex"])
            self.assertIn(("Codex", "Inicia sesión en la ventana que se abrió"), self.notes)
            app.select("claude")
            app.select("codex")
            self.assertEqual(self.opened, ["codex"], "not again within a few minutes: cycling past it opens nothing new")
            app.signin.login_at["codex"] -= config.LOGIN_COOLDOWN + 1
            app.select("codex")
            self.assertEqual(self.opened, ["codex", "codex"], "...but it comes back if it still isn't signed in")

    def test_a_provider_that_is_signed_in_opens_nothing(self):
        app = self.make()
        with capture_screen():
            app.select("codex")
            app.select("claude")
        self.assertEqual(self.opened, [])

    def test_it_is_for_every_provider(self):
        app = self.make()
        with fake_fetchers(claude=failing(SignInNeeded("Claude Code isn't signed in.")), codex=failing(UsageError("timed out"))):
            app.usage.fetch("claude")
            app.usage.fetch("codex")
        self.assertEqual(app.usage.needs_login, {"claude"}, "a timeout isn't a reason to sign in again")
        with capture_screen():
            app.select("codex")
            app.select("claude")
        self.assertEqual(self.opened, ["claude"])

    def test_a_missing_cli_says_how_to_install_it_instead(self):
        app = self.make()
        PROVIDERS["codex"].find_cli.return_value = None  # (not installed: nothing to open)
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
        self.assertIn("Install the Codex CLI", app.usage.errors["codex"])
        self.assertFalse(app.signin.error_usage("codex").signin, "there is nothing to sign in to yet")
        self.assertEqual(self.opened, [], "no window is opened for a CLI that isn't there")

    def test_no_terminal_still_leaves_a_message(self):
        self.outcome = "failed"
        app = self.make()
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
        self.assertIn("Couldn't open a terminal", app.usage.errors["codex"])

    def test_while_the_window_is_open_a_failed_read_keeps_the_instructions(self):
        app = self.make()
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
        note = app.usage.errors["codex"]
        with fake_fetchers(**SIGNED_OUT):
            app.usage.fetch("codex")
        self.assertEqual(app.usage.errors["codex"], note)
        self.assertTrue(app.signin.pending("codex"))
        app.signin.login_at["codex"] -= config.LOGIN_COOLDOWN + 1
        with fake_fetchers(**SIGNED_OUT):
            app.usage.fetch("codex")
        self.assertIn("Not signed in?", app.usage.errors["codex"], "after that, the plain reason")

    def test_once_signed_in_the_next_read_clears_everything(self):
        app = self.make()
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
        with fake_fetchers(codex=lambda: usage(title="Codex")):
            self.assertIsNotNone(app.usage.fetch("codex"))
        self.assertEqual((app.usage.errors, app.usage.needs_login, app.signin.notes, app.signin.pending("codex")),
                         ({}, set(), {}, False))

    def test_the_screen_gets_the_explanation_once_and_not_again_every_cycle(self):
        app = self.make()
        self.sign_out(app)
        with capture_screen():
            app.select("codex")
        shown = app.signin.error_usage("codex")
        self.assertTrue(shown.signin)
        self.assertEqual(shown.message, app.usage.errors["codex"])
        with capture_screen() as cap:
            app.screen.last_click = 0
            self.assertTrue(app.screen.upload("codex", shown))
        self.assertEqual([u.title for u in cap.errors], ["Codex"])
        self.assertIsNone(app.signin.error_usage("codex"), "it already says it")
        app.usage.errors["codex"] = "something else now"
        self.assertIsNotNone(app.signin.error_usage("codex"), "a different reason is worth saying")

    def test_a_provider_that_was_read_before_is_dimmed_not_replaced_by_an_error_screen(self):
        app = self.make()
        app.usage.last_good["claude"] = (usage(), time.monotonic())
        app.usage.errors["claude"] = "timed out"
        self.assertIsNone(app.signin.error_usage("claude"))

    def test_the_menu_can_always_open_a_sign_in(self):
        app = self.make()
        options = next(i for i in app.icon.menu.items if i.text == "Opciones")
        item = next(i for i in options.submenu.items if i.text == "Iniciar sesión")
        self.assertEqual([i.text for i in item.submenu.items], ["Claude", "Codex"])
        app.sign_in_action("claude")()  # signed in, as far as it knows: it's still what was asked for
        deadline = time.time() + 2
        while not self.opened and time.time() < deadline:  # (in the app the menu runs it in a thread)
            time.sleep(0.01)
        self.assertEqual(self.opened, ["claude"])

    def test_it_looks_again_often_while_a_sign_in_is_pending(self):
        app = self.make()
        self.sign_out(app)
        self.assertFalse(app.signin.pending("codex"))
        with capture_screen():
            app.select("codex")
        self.assertTrue(app.signin.pending("codex"))
        self.assertLess(config.LOGIN_POLL, app.scheduler.interval)

    def test_a_split_panel_for_a_provider_that_cant_be_read_keeps_its_place(self):
        app = self.make()
        app.usage.last_good["claude"] = (usage(), time.monotonic())
        app.usage.errors["codex"] = "Codex is not signed in."
        panels = app.scheduler.panels(SPLIT)
        self.assertEqual([p.title for p in panels], ["Claude", "Codex"])
        self.assertIsNone(panels[1].current.pct)
        app.usage.last_good.clear()
        self.assertEqual(app.scheduler.panels(SPLIT), [], "nothing readable at all: nothing to draw")
