from __future__ import annotations

from datetime import date
from pathlib import Path
import queue
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from database_utils import acknowledge_sync, get_sync_state, has_pending_sync


class SyncAppHarness:
    """Run real synchronization scheduling without a desktop or network."""

    schedule_cloud_sync = app.EstoqueApp.schedule_cloud_sync
    run_scheduled_cloud_sync = app.EstoqueApp.run_scheduled_cloud_sync
    retry_cloud_sync = app.EstoqueApp.retry_cloud_sync
    start_cloud_sync = app.EstoqueApp.start_cloud_sync
    poll_cloud_sync_events = app.EstoqueApp.poll_cloud_sync_events
    schedule_ui_task = app.EstoqueApp.schedule_ui_task
    show_page = app.EstoqueApp.show_page
    cloud_download = app.EstoqueApp.cloud_download
    cloud_upload = app.EstoqueApp.cloud_upload
    update_cloud_status = app.EstoqueApp.update_cloud_status

    def __init__(self, database):
        self.db = database
        self.cloud = SimpleNamespace(signed_in=True, email="teste@example.invalid", synchronize=Mock())
        self.cloud_settings = {}
        self.cloud_events = queue.Queue()
        self.cloud_sync_busy = False
        self.cloud_sync_pending = False
        self.cloud_sync_timer = None
        self.cloud_retry_timer = None
        self.cloud_sync_error = ""
        self.cloud_sync_confirmed = False
        self.cloud_status = Mock()
        self.sidebar_status = Mock()
        self.timers = {}
        self.timer_sequence = 0
        self._ui_jobs = {}
        self.settings = {}
        self.current_page = "movements"
        self.pages = {"movements": Mock(), "stock": Mock()}
        self.nav_buttons = {}
        self.save_cloud_settings = Mock()
        self.refresh_all = Mock()
        self.capture_interface_preferences = Mock()
        self.schedule_settings_save = Mock()
        self.refresh_stock = Mock()
        self.refresh_movement_page = Mock()
        self.refresh_defect_return_page = Mock()
        self.refresh_kit_conversion = Mock()
        self.refresh_simulation_page = Mock()
        self.refresh_counts = Mock()

    def after(self, delay, callback):
        self.timer_sequence += 1
        timer = str(self.timer_sequence)
        self.timers[timer] = (delay, callback)
        return timer

    def after_cancel(self, timer):
        self.timers.pop(timer, None)

    def run_timer(self, timer):
        _delay, callback = self.timers.pop(timer)
        callback()


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class MovementSyncSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = app.Database(Path(self.temporary.name) / "estoque.db")
        self.database.save_product({"name":"MARINHO", "category":"", "group_name":"2 PEÇAS", "variant":"", "unit":"un", "minimum":0, "photo":"", "notes":""})
        self.product_id = int(self.database.products()[0]["id"])
        acknowledge_sync(self.database.db, get_sync_state(self.database.db)[0])
        self.gui = SyncAppHarness(self.database)
        self.database.on_change = self.gui.schedule_cloud_sync

    def tearDown(self):
        self.database.db.close()
        self.temporary.cleanup()

    def add_movement(self):
        self.database.add_movement(self.product_id, "entrada", 1, date.today().isoformat(), "Teste isolado", "Teste")

    def test_save_is_committed_and_send_survives_immediate_navigation(self):
        self.add_movement()
        send_timer = self.gui.cloud_sync_timer
        self.assertIsNotNone(send_timer)
        self.assertEqual(self.gui.timers[send_timer][0], 0)
        self.gui.show_page("stock")
        self.assertIn(send_timer, self.gui.timers)
        reader = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(reader.execute("SELECT SUM(quantity) FROM movements").fetchone()[0], 1)
        finally:
            reader.close()
        self.assertTrue(has_pending_sync(self.database.db))
        self.gui.start_cloud_sync = Mock()
        self.gui.run_timer(send_timer)
        self.gui.start_cloud_sync.assert_called_once_with(silent=True)

    def test_many_sql_notifications_do_not_keep_postponing_upload(self):
        self.add_movement()
        first_timer = self.gui.cloud_sync_timer
        self.add_movement()
        self.assertEqual(self.gui.cloud_sync_timer, first_timer)
        self.assertEqual(len(self.gui.timers), 1)

    def test_save_during_upload_remains_pending_after_completion(self):
        self.gui.cloud_sync_busy = True
        self.add_movement()
        self.assertTrue(self.gui.cloud_sync_pending)
        self.assertIsNone(self.gui.cloud_sync_timer)
        self.gui.cloud_events.put(("success", {"action":"uploaded"}, True))
        self.gui.poll_cloud_sync_events()
        self.assertTrue(has_pending_sync(self.database.db))
        self.assertEqual(self.gui.timers[self.gui.cloud_sync_timer][0], 0)
        self.assertIn("aguardando envio", self.gui.cloud_status.set.call_args.args[0])

    def test_worker_connect_failure_is_queued_and_retried(self):
        with patch.object(app.threading, "Thread", ImmediateThread), patch.object(app.sqlite3, "connect", side_effect=sqlite3.OperationalError("busy")):
            self.gui.start_cloud_sync()
        self.gui.poll_cloud_sync_events()
        self.assertFalse(self.gui.cloud_sync_busy)
        self.assertIn("busy", self.gui.cloud_sync_error)
        self.assertIsNotNone(self.gui.cloud_retry_timer)
        self.assertIn("Nova tentativa automática", self.gui.cloud_status.set.call_args.args[0])

    def test_new_local_save_expedites_failed_connection_retry(self):
        self.gui.cloud_sync_busy = True
        self.gui.cloud_events.put(("error", "Sem internet", True))
        self.gui.poll_cloud_sync_events()
        retry_timer = self.gui.cloud_retry_timer
        self.add_movement()
        self.assertNotIn(retry_timer, self.gui.timers)
        self.assertIsNone(self.gui.cloud_retry_timer)
        self.assertEqual(self.gui.timers[self.gui.cloud_sync_timer][0], 0)

    def test_startup_uses_durable_pending_state(self):
        self.gui.cloud.synchronize.return_value = {"action":"unchanged"}
        with patch.object(app.threading, "Thread", ImmediateThread):
            self.gui.start_cloud_sync()
        self.assertFalse(self.gui.cloud.synchronize.call_args.args[1])
        self.gui.poll_cloud_sync_events()
        self.add_movement()
        with patch.object(app.threading, "Thread", ImmediateThread):
            self.gui.start_cloud_sync()
        self.assertTrue(self.gui.cloud.synchronize.call_args.args[1])

    def test_manual_download_cannot_replace_unsent_movement(self):
        self.add_movement()
        self.gui.start_cloud_sync = Mock()
        with patch.object(app.messagebox, "showinfo") as info, patch.object(app.messagebox, "askyesno") as confirm:
            self.gui.cloud_download()
        info.assert_called_once()
        confirm.assert_not_called()
        self.gui.start_cloud_sync.assert_not_called()
        self.assertEqual(self.database.stock(self.product_id), 1)

    def test_manual_upload_cannot_overlap_active_sync(self):
        self.gui.cloud_sync_busy = True
        self.gui.start_cloud_sync = Mock()
        with patch.object(app.messagebox, "showinfo") as info:
            self.gui.cloud_upload()
        info.assert_called_once()
        self.gui.start_cloud_sync.assert_not_called()

    def test_pending_result_keeps_sync_queue_alive(self):
        self.gui.cloud_sync_busy = True
        self.gui.cloud_events.put(("success", {"action":"pending", "pending":True}, True))
        self.gui.poll_cloud_sync_events()
        self.assertIsNotNone(self.gui.cloud_sync_timer)


if __name__ == "__main__":
    unittest.main()
