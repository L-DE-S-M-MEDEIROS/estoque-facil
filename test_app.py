from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from cloud_sync import CloudSync
from local_state import LocalCloudSession, LocalPreferences, LocalSimulationDraft, read_json_object
from premium_widgets import count_age_color, stock_quantity_color


class InventoryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = app.data_dir
        temporary_path = Path(self.temporary_directory.name)

        def temporary_data_dir() -> Path:
            (temporary_path / "fotos").mkdir(exist_ok=True)
            return temporary_path

        app.data_dir = temporary_data_dir
        self.db = app.Database()

    def tearDown(self):
        self.db.db.close()
        app.data_dir = self.original_data_dir
        self.temporary_directory.cleanup()

    def create_product(self, name="MARINHO", group="4 PEÇAS", variant="Azul marinho") -> int:
        self.db.save_product(
            {
                "name": name,
                "category": "Bolsa maternidade",
                "group_name": group,
                "variant": variant,
                "unit": "un",
                "minimum": 0,
                "photo": "",
                "notes": "",
            }
        )
        return int(self.db.products()[0]["id"])

    def test_group_crud_updates_products_and_blocks_in_use_deletion(self):
        group_id = self.db.save_group("4 PEÇAS")
        product_id = self.create_product()
        self.db.save_group("QUATRO PEÇAS", group_id)
        self.assertEqual(self.db.product(product_id)["group_name"], "QUATRO PEÇAS")
        with self.assertRaisesRegex(ValueError, "sendo usado"):
            self.db.delete_group(group_id)

    def test_existing_product_groups_are_imported_during_migration(self):
        self.create_product(group="2 PEÇAS")
        self.db.db.execute("DROP TABLE product_groups")
        self.db.db.commit()
        self.db.db.close()
        self.db = app.Database()
        self.assertIn("2 PEÇAS", self.db.groups())

    def test_product_search_matches_partial_text_without_accents(self):
        product_id = self.create_product()
        product = self.db.product(product_id)
        self.assertTrue(app.product_matches_search(product, "mar"))
        self.assertTrue(app.product_matches_search(product, "4 pec"))
        self.assertTrue(app.product_matches_search(product, "matern"))
        self.assertFalse(app.product_matches_search(product, "caramelo"))

    def test_default_operations_can_be_renamed_and_hidden(self):
        entry = self.db.operation("entrada")
        self.db.save_operation("Recebimento", "negative", int(entry["id"]))
        self.assertEqual(self.db.operation("entrada")["name"], "Recebimento")
        self.assertEqual(self.db.operation("entrada")["effect"], "positive")
        self.db.delete_operation(int(entry["id"]))
        self.assertNotIn("Recebimento", [row["name"] for row in self.db.operations()])

    def test_hidden_inventory_operation_still_supports_counting(self):
        product_id = self.create_product()
        inventory = self.db.operation("inventario")
        self.db.delete_operation(int(inventory["id"]))
        self.db.add_movement(product_id, "inventario", 12, date.today().isoformat(), "Contagem", "Teste")
        self.assertEqual(self.db.stock(product_id), 12)

    def test_relative_count_dates(self):
        today = date(2026, 8, 20)
        self.assertEqual(app.relative_past_date(today.isoformat(), today), "Hoje")
        self.assertEqual(app.relative_past_date((today - timedelta(days=3)).isoformat(), today), "3 dias atrás")
        self.assertEqual(app.relative_past_date((today - timedelta(days=14)).isoformat(), today), "2 semanas atrás")
        self.assertEqual(app.relative_past_date("", today), "Nunca contado")

    def test_count_age_colors_become_more_urgent(self):
        self.assertEqual(count_age_color(0)[0], "#2478C4")
        self.assertEqual(count_age_color(3)[0], "#27845E")
        self.assertEqual(count_age_color(14)[0], "#A87500")
        self.assertEqual(count_age_color(45)[0], "#D45A32")
        self.assertEqual(count_age_color(None)[0], "#8F2433")

    def test_batch_movements_keep_stock_consistent(self):
        product_id = self.create_product()
        self.db.add_movement(product_id, "inventario", 10, date.today().isoformat(), "Inicial", "Teste")
        batch_id = self.db.add_movement_batch("saida", [(product_id, 3)], date.today().isoformat(), "Venda", "Teste")
        self.assertGreater(batch_id, 0)
        self.assertEqual(self.db.stock(product_id), 7)

    def test_outgoing_movement_can_leave_stock_negative_and_reports_product(self):
        product_id = self.create_product()
        self.db.add_movement_batch("saida", [(product_id, 3)], date.today().isoformat(), "Venda antecipada", "Teste")

        self.assertEqual(self.db.stock(product_id), -3)
        negative_products = self.db.negative_stock_products()
        self.assertEqual(len(negative_products), 1)
        self.assertEqual(negative_products[0]["id"], product_id)
        self.assertEqual(negative_products[0]["stock"], -3)

        self.db.add_movement_batch("entrada", [(product_id, 3)], date.today().isoformat(), "Correção", "Teste")
        self.assertEqual(self.db.stock(product_id), 0)
        self.assertEqual(self.db.negative_stock_products(), [])

    def test_negative_stock_uses_dark_wine_color(self):
        self.assertEqual(stock_quantity_color(-1), ("#5A0B1A", "#5A0B1A"))

    def test_simulation_projects_entry_and_exit_without_changing_inventory(self):
        product_id = self.create_product()
        self.db.add_movement(product_id, "inventario", 10, date.today().isoformat(), "Inicial", "Teste")

        self.assertEqual(app.simulated_stock(self.db.stock(product_id), 4, "entrada"), 14)
        self.assertEqual(app.simulated_stock(self.db.stock(product_id), 12, "saida"), -2)
        self.assertEqual(self.db.stock(product_id), 10)

    def test_simulation_rejects_invalid_quantity_and_operation(self):
        with self.assertRaisesRegex(ValueError, "maior que zero"):
            app.simulated_stock(10, 0, "saida")
        with self.assertRaisesRegex(ValueError, "inválida"):
            app.simulated_stock(10, 2, "ajuste")

    def test_cloud_payload_contains_inventory_and_photo(self):
        photo = app.data_dir() / "fotos" / "produto.png"
        photo.write_bytes(b"imagem")
        product_id = self.create_product()
        product = dict(self.db.product(product_id))
        product["photo"] = str(photo)
        self.db.save_product(product, product_id)
        draft = LocalSimulationDraft(app.data_dir() / "simulation-draft.json")
        draft.values = {"operation": "saida", "items": [{"product_id": product_id, "quantity": 99}]}
        draft.save()
        sync = CloudSync(app.data_dir(), {})
        payload = sync.export_payload(self.db.db)
        self.assertEqual(payload["format"], 1)
        self.assertEqual(set(payload), {"format", "app", "exported_at", "tables", "photos"})
        self.assertEqual(payload["tables"]["products"][0]["photo"], "produto.png")
        self.assertIn("produto.png", payload["photos"])
        self.assertNotIn("simulation", payload)


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_legacy_settings_are_split_without_sending_ui_preferences_to_cloud_session(self):
        legacy = {
            "theme": "Dark",
            "last_page": "movements",
            "stock_search": "bolsa azul",
            "cloud_access_token": "secret-token",
            "cloud_user_id": "user-123",
        }
        preferences = LocalPreferences(self.folder / "ui-preferences.json", legacy)
        session = LocalCloudSession(self.folder / "cloud-session.json", legacy)

        self.assertEqual(preferences.values["theme"], "Dark")
        self.assertEqual(preferences.values["last_page"], "movements")
        self.assertNotIn("cloud_access_token", read_json_object(preferences.path)["preferences"])
        self.assertEqual(session.values["cloud_access_token"], "secret-token")
        self.assertNotIn("theme", read_json_object(session.path)["session"])

    def test_local_preferences_survive_restart_and_invalid_values_use_safe_defaults(self):
        path = self.folder / "ui-preferences.json"
        preferences = LocalPreferences(path)
        preferences.values.update({"theme": "Dark", "count_filter": "pendentes", "last_page": "count"})
        preferences.save()

        restored = LocalPreferences(path)
        self.assertEqual(restored.values["theme"], "Dark")
        self.assertEqual(restored.values["count_filter"], "pendentes")
        self.assertEqual(restored.values["last_page"], "count")

        restored.values.update({"theme": "Inválido", "count_filter": "qualquer", "last_page": "desconhecida"})
        restored.save()
        self.assertEqual(restored.values["theme"], "Light")
        self.assertEqual(restored.values["count_filter"], "todos")
        self.assertEqual(restored.values["last_page"], "stock")

    def test_signed_out_cloud_session_is_not_reimported_from_legacy_settings(self):
        path = self.folder / "cloud-session.json"
        legacy = {"cloud_access_token": "old-token", "cloud_user_id": "old-user"}
        session = LocalCloudSession(path, legacy)
        session.values.clear()
        session.save()

        restored = LocalCloudSession(path, legacy)
        self.assertEqual(restored.values, {})

    def test_simulation_draft_is_local_persistent_and_sanitized(self):
        path = self.folder / "simulation-draft.json"
        draft = LocalSimulationDraft(path)
        draft.values = {
            "operation": "saida",
            "items": [
                {"product_id": 7, "quantity": 12},
                {"product_id": 8, "quantity": -1},
                {"product_id": "inválido", "quantity": 3},
                {"product_id": 7, "quantity": 15},
            ],
        }
        draft.save()

        restored = LocalSimulationDraft(path)
        self.assertEqual(restored.values, {"operation": "saida", "items": [{"product_id": 7, "quantity": 15.0}]})
        self.assertEqual(read_json_object(path)["simulation"], restored.values)

    def test_simulation_can_be_restored_as_last_local_page(self):
        path = self.folder / "ui-preferences.json"
        preferences = LocalPreferences(path)
        preferences.values["last_page"] = "simulation"
        preferences.save()

        self.assertEqual(LocalPreferences(path).values["last_page"], "simulation")


class SharedCloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.settings = {
            "cloud_access_token": "token",
            "cloud_user_id": "00000000-0000-0000-0000-000000000001",
            "cloud_device_id": "00000000-0000-0000-0000-000000000002",
        }
        self.sync = CloudSync(Path(self.temporary_directory.name), self.settings)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def payload(product_name=""):
        tables = {name: [] for name in ("operation_types", "users", "product_groups", "products", "movement_batches", "movements")}
        if product_name:
            tables["products"] = [{"id": 1, "name": product_name}]
        return {"format": 1, "app": "Estoque Bolsas Baby", "exported_at": "agora", "tables": tables, "photos": {}}

    def test_payload_fingerprint_ignores_export_time(self):
        first = self.payload("MARINHO")
        second = dict(first, exported_at="depois")
        self.assertEqual(self.sync.payload_fingerprint(first), self.sync.payload_fingerprint(second))

    def test_upload_targets_shared_workspace_for_every_authenticated_user(self):
        payload = self.payload("CARAMELO")
        with patch.object(self.sync, "_request", return_value=[{"revision": 3, "updated_at": "2026-08-21T15:00:00+00:00"}]) as request:
            self.sync._upload_payload(payload, 3)
        path = request.call_args.args[0]
        body = request.call_args.kwargs["body"]
        self.assertIn("shared_inventory_snapshot", path)
        self.assertEqual(body["workspace_key"], "bolsas-baby")
        self.assertEqual(body["updated_by"], self.settings["cloud_user_id"])
        self.assertNotIn("owner_id", body)

    def test_remote_change_is_downloaded_when_local_matches_last_sync(self):
        local = self.payload("MARINHO")
        remote_payload = self.payload("VERDE")
        self.settings["cloud_last_fingerprint"] = self.sync.payload_fingerprint(local)
        remote = {"payload": remote_payload, "revision": 4, "updated_at": "2026-08-21T15:00:00+00:00"}
        with patch.object(self.sync, "export_payload", return_value=local), patch.object(self.sync, "remote_snapshot", return_value=remote), patch.object(self.sync, "_download_snapshot", return_value=remote["updated_at"]) as download:
            result = self.sync.synchronize(object())
        self.assertEqual(result["action"], "downloaded")
        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
