"""Float lifecycle regressions with fake Tk/Win32 and a temporary database only."""
import unittest
from unittest import mock

from tests.support import headless_app


class TestFloatStartup(unittest.TestCase):
    def app(self, consent=""):
        return headless_app(overlays="panel", main_window="real",
                            config={"ui.overlay_topmost_consent": consent})

    def test_first_launch_asks_inside_float_without_opening_main(self):
        with self.app() as app:
            self.assertTrue(app.start_floating_ui())
            panel = app.reading_panel
            self.assertTrue(panel.visible)
            self.assertTrue(panel.is_open())
            self.assertTrue(panel.consent_card_visible())
            self.assertFalse(app.effective_topmost())
            self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_answer_is_saved_and_does_not_move_or_hide_panel(self):
        for allow in (True, False):
            with self.subTest(allow=allow), self.app() as app:
                app.start_floating_ui()
                panel = app.reading_panel
                rect = panel.state.panel_rect
                panel.set_overlay_topmost_consent(allow)
                self.assertIs(app.config.overlay_topmost_consent, allow)
                self.assertEqual(app.effective_topmost(), allow)
                self.assertEqual(panel.topmost(), allow)
                self.assertFalse(panel.consent_card_visible())
                self.assertTrue(panel.is_open())
                self.assertEqual(panel.state.panel_rect, rect)
                panel.collapse_to_dock(explicit=True)
                app.start_floating_ui()
                self.assertFalse(panel.is_open())

    def test_remembered_answer_starts_as_dock(self):
        for consent in ("0", "1"):
            with self.subTest(consent=consent), self.app(consent) as app:
                self.assertTrue(app.start_floating_ui())
                self.assertTrue(app.reading_panel.visible)
                self.assertFalse(app.reading_panel.is_open())
                self.assertFalse(app.reading_panel.consent_card_visible())
                self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_temporary_block_restores_state_and_geometry(self):
        for expanded in (True, False):
            with self.subTest(expanded=expanded), self.app("1") as app:
                app.start_floating_ui()
                panel = app.reading_panel
                if expanded:
                    panel.expand(explicit=True)
                rect = panel.state.panel_rect
                dock = panel.state.dock_pos
                app.config.set_bool("gate.game_mode", True)
                app._apply_gate(force=True)
                self.assertFalse(panel.visible)
                self.assertFalse(app.effective_topmost())
                app.config.set_bool("gate.game_mode", False)
                app._apply_gate(force=True)
                self.assertTrue(panel.visible)
                self.assertEqual(panel.is_open(), expanded)
                self.assertEqual(panel.state.panel_rect, rect)
                self.assertEqual(panel.state.dock_pos, dock)
                self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_user_close_stays_closed_until_explicit_activation(self):
        with self.app("1") as app:
            app.start_floating_ui()
            panel = app.reading_panel
            panel.close_by_user()
            app.start_floating_ui()
            app._apply_gate(force=True)
            self.assertFalse(panel.visible)
            with mock.patch.object(app, "_notify_already_running") as notice:
                self.assertTrue(app.request_open_main("activation"))
                notice.assert_called_once()
            self.assertTrue(panel.visible)
            self.assertFalse(panel.closed_by_user)
            self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_outside_click_and_reactivation_preserve_expansion(self):
        with self.app("1") as app:
            app.start_floating_ui()
            panel = app.reading_panel
            panel.expand(explicit=True)
            rect = panel.state.panel_rect
            app._handle_overlay_press({"is_overlay": False})
            app._apply_gate(force=True)
            with mock.patch.object(app, "_notify_already_running"):
                app.request_open_main("activation")
            self.assertTrue(panel.is_open())
            self.assertEqual(panel.state.panel_rect, rect)
            self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_tray_toggle_removes_unanswered_card_without_collapsing(self):
        with self.app() as app:
            app.start_floating_ui()
            app._handle_event("tray_topmost", None)
            self.assertTrue(app.effective_topmost())
            self.assertFalse(app.reading_panel.consent_card_visible())
            self.assertTrue(app.reading_panel.is_open())

    def test_unanswered_card_does_not_force_reexpansion(self):
        with self.app() as app:
            app.start_floating_ui()
            app.reading_panel.collapse_to_dock(explicit=True)
            app.config.set_bool("gate.game_mode", True)
            app._apply_gate(force=True)
            app.config.set_bool("gate.game_mode", False)
            app._apply_gate(force=True)
            self.assertTrue(app.reading_panel.visible)
            self.assertFalse(app.reading_panel.is_open())

    def test_activation_waits_until_float_is_actually_restored(self):
        with self.app("1") as app:
            with mock.patch.object(app, "restore_floating_ui", return_value=False), \
                    mock.patch.object(app, "_notify_already_running") as notice:
                self.assertFalse(app.request_open_main("activation"))
                self.assertTrue(app._pending_open_main)
                notice.assert_not_called()
            with mock.patch.object(app, "_notify_already_running") as notice:
                self.assertTrue(app._drain_activation())
                notice.assert_called_once()
            self.assertFalse(app._pending_open_main)
            self.assertTrue(app.reading_panel.visible)
