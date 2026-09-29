from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
import cloud_sync
from cloud_sync import CloudSync, CloudSyncError
from database_utils import configure_database_connection, get_sync_state, has_pending_sync


class SnapshotTransport:
    """Deterministic network pauses while SQLite stays real and independently open."""

    def __init__(self):
        self.snapshot = None
        self.on_get = None
        self.on_put = None
        self.uploads = []

    def request(self, path, method="GET", body=None, **kwargs):
        if method == "GET":
            response = copy.deepcopy(self.snapshot)
            callback, self.on_get = self.on_get, None
            if callback:
                callback()
            return response
        if method == "PUT":
            captured = copy.deepcopy(body)
            callback, self.on_put = self.on_put, None
            if callback:
                callback()
            self.snapshot = captured
            self.uploads.append(captured)
            return copy.deepcopy(captured)
        raise AssertionError(f"Unexpected test transport method: {method}")


class DurableMovementSyncTests(unittest.TestCase):
    """A saved movement must survive page changes and an in-flight cloud request."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.folder = Path(self.temporary_directory.name)
        (self.folder / "fotos").mkdir()
        self.data_dir_patch = patch.object(app, "data_dir", return_value=self.folder)
        self.data_dir_patch.start()
        self.addCleanup(self.data_dir_patch.stop)
        self.db = app.Database()
        self.worker = self.open_worker()
        self.addCleanup(self.close_connections)
        self.db.save_product({
            "name": "MARINHO", "category": "Bolsa maternidade",
            "group_name": "4 PEÇAS", "variant": "Azul marinho", "unit": "un",
            "minimum": 0, "photo": "", "notes": "",
        })
        self.product_id = int(self.db.products()[0]["id"])
        self.db.add_movement(
            self.product_id, "entrada", 20, date.today().isoformat(), "Inicial", "Teste"
        )
        self.settings = {"cloud_provider": "firebase", "cloud_user_id": "test-user"}
        self.cloud = CloudSync(self.folder, self.settings)
        self.transport = SnapshotTransport()
        self.request_patch = patch.object(self.cloud, "_request", side_effect=self.transport.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        result = self.cloud.synchronize(self.worker)
        self.assertEqual(result["action"], "uploaded")
        self.assertFalse(has_pending_sync(self.worker))

    def open_worker(self):
        connection = sqlite3.connect(self.db.path, timeout=5)
        configure_database_connection(connection)
        return connection

    def close_connections(self):
        self.worker.close()
        self.db.db.close()

    def sale(self, quantity=3):
        self.db.add_movement(
            self.product_id, "saida", quantity, date.today().isoformat(), "Baixa rápida", "Teste"
        )

    def local_stock(self):
        return float(self.worker.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM movements WHERE product_id=?",
            (self.product_id,),
        ).fetchone()[0])

    def remote_stock(self):
        return sum(
            float(row["quantity"])
            for row in self.transport.snapshot["payload"]["tables"]["movements"]
            if row["product_id"] == self.product_id
        )

    def remote_change(self):
        snapshot = self.transport.snapshot
        snapshot["payload"]["tables"]["products"][0]["notes"] = "Alterado em outra máquina"
        snapshot["revision"] += 1
        snapshot["updated_at"] = "2099-12-31T23:59:59+00:00"

    def test_movement_committed_during_remote_get_cannot_be_replaced(self):
        self.remote_change()
        self.transport.on_get = self.sale

        result = self.cloud.synchronize(self.worker)

        self.assertEqual(result["action"], "pending")
        self.assertEqual(self.local_stock(), 17)
        self.assertEqual(self.db.product(self.product_id)["notes"], "")
        self.assertTrue(has_pending_sync(self.worker))
        retry = self.cloud.synchronize(self.worker)
        self.assertEqual(retry["action"], "uploaded")
        self.assertEqual(self.remote_stock(), 17)
        self.assertFalse(has_pending_sync(self.worker))

    def test_second_movement_during_upload_is_not_acknowledged_early(self):
        self.sale(3)
        self.transport.on_put = lambda: self.sale(5)

        result = self.cloud.synchronize(self.worker)

        self.assertEqual(result["action"], "uploaded")
        self.assertTrue(result.get("pending"))
        self.assertEqual(self.local_stock(), 12)
        self.assertEqual(self.remote_stock(), 17)
        self.assertTrue(has_pending_sync(self.worker))
        retry = self.cloud.synchronize(self.worker)
        self.assertEqual(retry["action"], "uploaded")
        self.assertEqual(self.remote_stock(), 12)
        self.assertEqual(len(self.transport.snapshot["payload"]["tables"]["movements"]), 3)
        self.assertFalse(has_pending_sync(self.worker))

    def test_failed_upload_survives_restart_and_newer_remote_timestamp(self):
        persisted_settings = dict(self.settings)
        self.sale(3)

        def fail_upload():
            raise CloudSyncError("Network unavailable during test")

        self.transport.on_put = fail_upload
        with self.assertRaises(CloudSyncError):
            self.cloud.synchronize(self.worker)
        self.assertTrue(has_pending_sync(self.worker))
        self.worker.close()
        self.db.db.close()
        self.db = app.Database()
        self.worker = self.open_worker()
        self.cloud.settings.clear()
        self.cloud.settings.update(persisted_settings)
        self.remote_change()

        self.assertTrue(has_pending_sync(self.worker))
        result = self.cloud.synchronize(self.worker)

        self.assertEqual(result["action"], "uploaded")
        self.assertEqual(self.local_stock(), 17)
        self.assertEqual(self.remote_stock(), 17)
        self.assertFalse(has_pending_sync(self.worker))

    def test_rolled_back_write_does_not_enqueue_a_durable_change(self):
        before = get_sync_state(self.worker)
        with self.assertRaisesRegex(ValueError, "cancelled"):
            with self.db.db:
                self.db.db.execute(
                    "UPDATE products SET notes='must not persist' WHERE id=?", (self.product_id,)
                )
                raise ValueError("cancelled")

        self.assertEqual(get_sync_state(self.worker), before)
        self.assertFalse(has_pending_sync(self.worker))
        self.assertEqual(self.db.product(self.product_id)["notes"], "")
        uploads_before = len(self.transport.uploads)
        self.assertEqual(self.cloud.synchronize(self.worker)["action"], "unchanged")
        self.assertEqual(len(self.transport.uploads), uploads_before)

    def test_remote_changes_still_download_when_local_database_is_clean(self):
        self.remote_change()

        result = self.cloud.synchronize(self.worker)

        self.assertEqual(result["action"], "downloaded")
        self.assertEqual(self.db.product(self.product_id)["notes"], "Alterado em outra máquina")
        self.assertEqual(self.local_stock(), 20)
        self.assertFalse(has_pending_sync(self.worker))
        self.assertEqual(self.cloud.synchronize(self.worker)["action"], "unchanged")

    def test_invalid_download_rolls_back_data_and_sync_revision_together(self):
        local_before = self.cloud.export_payload(self.worker)["tables"]
        state_before = get_sync_state(self.worker)
        settings_before = dict(self.settings)
        self.remote_change()
        self.transport.snapshot["payload"]["tables"]["movements"][0]["product_id"] = 999999

        with self.assertRaises((CloudSyncError, sqlite3.IntegrityError)):
            self.cloud.synchronize(self.worker)

        self.assertEqual(self.cloud.export_payload(self.worker)["tables"], local_before)
        self.assertEqual(get_sync_state(self.worker), state_before)
        self.assertEqual(self.settings, settings_before)
        self.assertEqual(list(self.worker.execute("PRAGMA foreign_key_check")), [])

    def test_download_guard_checks_revision_again_at_write_boundary(self):
        expected_revision = get_sync_state(self.worker)[0]
        self.remote_change()
        self.sale(3)
        state_before = get_sync_state(self.worker)

        with self.assertRaises(cloud_sync.LocalChangesPending):
            self.cloud._download_snapshot(
                self.worker, self.transport.snapshot, expected_revision=expected_revision
            )

        self.assertEqual(self.local_stock(), 17)
        self.assertEqual(get_sync_state(self.worker), state_before)
        self.assertTrue(has_pending_sync(self.worker))


if __name__ == "__main__":
    unittest.main()
