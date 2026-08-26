from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
from pypdf import PdfReader
from cloud_sync import CloudSync, CloudSyncError, TABLES
from premium_icons import app_icon, application_icon_path
from PIL import Image
from local_state import LocalCloudSession, LocalPreferences, LocalSimulationDraft, read_json_object
from premium_widgets import canvas_wheel_impulse, count_age_color, stock_quantity_color, tree_wheel_units


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
        self.assertTrue(app.product_matches_search(product, "4 pecas marinho"))
        self.assertTrue(app.product_matches_search(product, "matern"))
        self.assertFalse(app.product_matches_search(product, "caramelo"))

    def test_duplicate_product_in_same_group_is_blocked_after_normalization(self):
        product_id = self.create_product(name="MARÍNHO", group="4 PEÇAS")
        duplicate = {
            "name": "  marinho  ",
            "category": "Outra categoria",
            "group_name": "  4 pecas ",
            "variant": "Outra variação",
            "unit": "un",
            "minimum": 0,
            "photo": "",
            "notes": "",
        }
        with self.assertRaisesRegex(ValueError, "Já existe o produto"):
            self.db.save_product(duplicate)
        self.db.save_product(dict(self.db.product(product_id)), product_id)
        self.assertEqual(len(self.db.products()), 1)

    def test_same_product_is_allowed_in_different_groups(self):
        self.create_product(name="MARINHO", group="2 PEÇAS")
        self.db.save_product({"name":"marinho","category":"Bolsa maternidade","group_name":"4 peças","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        self.assertEqual(len(self.db.products()), 2)

    def test_database_trigger_also_blocks_direct_duplicate_insert(self):
        self.create_product(name="CARAMELO", group="FITA 2 PEÇAS")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "duplicate_product_same_group"):
            with self.db.db:
                self.db.db.execute("""INSERT INTO products(name,category,group_name,variant,unit,minimum,photo,notes,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (" caramelo ","","fita 2 pecas","","un",0,"","",datetime.now().isoformat()))

    def test_count_product_picker_preference_is_persistent(self):
        path = Path(self.temporary_directory.name) / "ui-preferences.json"
        preferences = LocalPreferences(path)
        preferences.values["count_products_expanded"] = True
        preferences.save()
        self.assertTrue(LocalPreferences(path).values["count_products_expanded"])

    def test_default_operations_can_be_renamed_and_hidden(self):
        entry = self.db.operation("entrada")
        self.db.save_operation("Recebimento", "negative", int(entry["id"]))
        self.assertEqual(self.db.operation("entrada")["name"], "Recebimento")
        self.assertEqual(self.db.operation("entrada")["effect"], "positive")
        self.db.delete_operation(int(entry["id"]))
        self.assertNotIn("Recebimento", [row["name"] for row in self.db.operations()])

    def test_internal_kit_operations_do_not_mix_with_custom_operations(self):
        self.db.save_operation("MONTAGEM", "positive")
        visible_names = [row["name"] for row in self.db.operations()]
        self.assertIn("MONTAGEM", visible_names)
        self.assertNotIn("Montagem de kits", visible_names)
        self.assertNotIn("Desmontagem de kits", visible_names)

        with self.db.db:
            self.db.db.execute("DELETE FROM operation_types WHERE legacy_type IN ('kit_assembly','kit_disassembly')")
        self.assertTrue(self.db.ensure_kit_operations())
        self.assertEqual(self.db.operation("kit_assembly")["name"], "Montagem de kits")
        self.assertEqual(self.db.operation("kit_disassembly")["name"], "Desmontagem de kits")

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

    def test_kit_helpers_match_only_the_same_family_color_and_variation(self):
        selected = {"id": 5, "group_name": "FITA 5 PEÇAS", "name": "VERDE", "variant": "LISO"}
        products = [
            {"id": 1, "group_name": "FITA 2 PEÇAS", "name": "VERDE", "variant": "LISO"},
            {"id": 2, "group_name": "FITA 4 PEÇAS", "name": "VERDE", "variant": "LISO"},
            {"id": 3, "group_name": "FITA 4 PEÇAS", "name": "MARINHO", "variant": "LISO"},
            {"id": 4, "group_name": "CASINHA 4 PEÇAS", "name": "VERDE", "variant": "LISO"},
            selected,
        ]

        self.assertEqual(app.kit_piece_count(selected), 5)
        self.assertEqual(app.kit_group_family(selected), "fita")
        self.assertEqual([row["id"] for row in app.compatible_smaller_kits(selected, products)], [1, 2])

    def test_kit_assembly_moves_one_for_one_in_a_closed_batch(self):
        source_id = self.create_product(name="VERDE", group="FITA 2 PEÇAS", variant="")
        self.db.save_product({"name":"VERDE","category":"Bolsa maternidade","group_name":"FITA 5 PEÇAS","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        target_id = int(self.db.db.execute("SELECT id FROM products WHERE group_name='FITA 5 PEÇAS'").fetchone()["id"])
        self.db.add_movement(source_id, "inventario", 3, "2026-08-25", "Inicial", "Ana")

        batch_id = self.db.add_kit_conversion("montagem", source_id, target_id, 1, "2026-08-26", "Pedido 20", "Ana")

        self.assertEqual(self.db.stock(source_id), 2)
        self.assertEqual(self.db.stock(target_id), 1)
        conversion = self.db.kit_conversion_batch(batch_id)
        self.assertEqual(conversion["mode"], "montagem")
        self.assertEqual(float(conversion["source"]["quantity"]), -1)
        self.assertEqual(float(conversion["target"]["quantity"]), 1)
        history = next(row for row in self.db.movement_history() if row["history_key"] == f"batch:{batch_id}")
        self.assertEqual(history["operation_name"], "Montagem de kits")
        self.assertEqual(history["item_count"], 2)

    def test_kit_disassembly_removes_five_piece_and_adds_two_piece(self):
        target_id = self.create_product(name="VERDE", group="2 PEÇAS", variant="")
        self.db.save_product({"name":"VERDE","category":"Bolsa maternidade","group_name":"5 PEÇAS","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        source_id = int(self.db.db.execute("SELECT id FROM products WHERE group_name='5 PEÇAS'").fetchone()["id"])
        self.db.add_movement(source_id, "inventario", 4, "2026-08-25", "Inicial", "Ana")

        batch_id = self.db.add_kit_conversion("desmembramento", source_id, target_id, 1, "2026-08-26", "", "Ana")

        self.assertEqual(self.db.stock(source_id), 3)
        self.assertEqual(self.db.stock(target_id), 1)
        self.assertEqual(self.db.kit_conversion_batch(batch_id)["mode"], "desmembramento")

    def test_kit_conversion_blocks_different_color_wrong_direction_and_fraction(self):
        lower_id = self.create_product(name="VERDE", group="2 PEÇAS", variant="")
        self.db.save_product({"name":"MARINHO","category":"Bolsa maternidade","group_name":"5 PEÇAS","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        higher_id = int(self.db.db.execute("SELECT id FROM products WHERE group_name='5 PEÇAS'").fetchone()["id"])
        with self.assertRaisesRegex(ValueError, "mesma cor/variação"):
            self.db.add_kit_conversion("montagem", lower_id, higher_id, 1, "2026-08-26", "", "Ana")

        self.db.save_product({"name":"VERDE","category":"Bolsa maternidade","group_name":"5 PEÇAS","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        green_higher_id = int(self.db.db.execute("SELECT id FROM products WHERE group_name='5 PEÇAS' AND name='VERDE'").fetchone()["id"])
        with self.assertRaisesRegex(ValueError, "kit menor como origem"):
            self.db.add_kit_conversion("montagem", green_higher_id, lower_id, 1, "2026-08-26", "", "Ana")
        with self.assertRaisesRegex(ValueError, "inteiro maior que zero"):
            self.db.add_kit_conversion("montagem", lower_id, green_higher_id, 1.5, "2026-08-26", "", "Ana")

    def test_kit_conversion_can_be_edited_and_deleted_as_one_movement(self):
        lower_id = self.create_product(name="VERDE", group="2 PEÇAS", variant="")
        self.db.save_product({"name":"VERDE","category":"Bolsa maternidade","group_name":"4 PEÇAS","variant":"","unit":"un","minimum":0,"photo":"","notes":""})
        higher_id = int(self.db.db.execute("SELECT id FROM products WHERE group_name='4 PEÇAS'").fetchone()["id"])
        self.db.add_movement(lower_id, "inventario", 5, "2026-08-24", "Inicial", "Ana")
        batch_id = self.db.add_kit_conversion("montagem", lower_id, higher_id, 1, "2026-08-25", "", "Ana")

        self.db.update_kit_conversion(batch_id, "montagem", lower_id, higher_id, 2, "2026-08-26", "Revisado", "Bia")
        self.assertEqual(self.db.stock(lower_id), 3)
        self.assertEqual(self.db.stock(higher_id), 2)
        self.assertEqual(self.db.movement_batch(batch_id)["performed_by"], "Bia")

        self.db.delete_movement_batch(batch_id)
        self.assertEqual(self.db.stock(lower_id), 5)
        self.assertEqual(self.db.stock(higher_id), 0)

    def test_closed_movement_history_updates_and_deletes_whole_batch(self):
        first_id = self.create_product(name="MARINHO")
        self.db.save_product({"name":"VERDE","category":"Bolsa maternidade","group_name":"4 PEÇAS","variant":"Verde","unit":"un","minimum":0,"photo":"","notes":""})
        second_id = int(self.db.db.execute("SELECT MAX(id) id FROM products").fetchone()["id"])
        self.db.add_movement_batch("inventario", [(first_id, 10), (second_id, 8)], "2026-08-20", "Contagem inicial", "Ana")
        sale_id = self.db.add_movement_batch("saida", [(first_id, 3), (second_id, 2)], "2026-08-21", "Pedido 15", "Vinicius")

        history = self.db.movement_history()
        sale = next(item for item in history if item["history_key"] == f"batch:{sale_id}")
        self.assertEqual(sale["item_count"], 2)
        self.assertIn("MARINHO", sale["product_summary"])
        self.assertIn("VERDE", sale["product_summary"])

        self.db.update_movement_batch(sale_id, "saida", [(first_id, 4), (second_id, 1)], "2026-08-22", "Pedido corrigido", "Larissa")
        self.assertEqual(self.db.stock(first_id), 6)
        self.assertEqual(self.db.stock(second_id), 7)
        updated = self.db.movement_batch(sale_id)
        self.assertEqual(updated["reason"], "Pedido corrigido")
        self.assertEqual(updated["performed_by"], "Larissa")

        self.db.delete_movement_batch(sale_id)
        self.assertEqual(self.db.stock(first_id), 10)
        self.assertEqual(self.db.stock(second_id), 8)
        self.assertNotIn(f"batch:{sale_id}", [item["history_key"] for item in self.db.movement_history()])

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

    def test_simulation_comparison_includes_complete_current_and_projected_stock(self):
        products = [
            {"id": 1, "name": "MARINHO", "stock": 10, "unit": "un"},
            {"id": 2, "name": "CARAMELO", "stock": 7, "unit": "un"},
        ]

        comparison = app.simulation_stock_comparison(
            products,
            [{"product_id": 1, "quantity": 4}],
            "saida",
        )

        self.assertEqual(len(comparison), 2)
        self.assertEqual(comparison[0]["current"], 10)
        self.assertEqual(comparison[0]["projected"], 6)
        self.assertEqual(comparison[1]["current"], 7)
        self.assertEqual(comparison[1]["projected"], 7)
        self.assertIsNone(comparison[1]["quantity"])

    def test_simulation_selected_rows_include_only_the_assembled_set(self):
        products = [
            {"id":1,"name":"MARINHO","group_name":"4 PEÇAS","variant":"","category":"","stock":80,"unit":"un"},
            {"id":2,"name":"CARAMELO","group_name":"4 PEÇAS","variant":"","category":"","stock":50,"unit":"un"},
        ]
        rows = app.simulation_selected_rows(products,[{"product_id":1,"quantity":30}],"saida")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["product_id"],1)
        self.assertEqual(rows[0]["projected"],50)

    def test_simulation_print_pdf_contains_only_product_and_simulated_quantity(self):
        products = [{"id":1,"name":"MARINHO","group_name":"4 PEÇAS","variant":"","category":"","stock":80,"unit":"un"}]
        rows = app.simulation_selected_rows(products,[{"product_id":1,"quantity":30}],"saida")
        output = Path(self.temporary_directory.name) / "lista-simulacao.pdf"
        app.build_simulation_print_pdf(output,rows,"saida",datetime(2026,8,25,14,30))
        text = "\n".join(page.extract_text() or "" for page in PdfReader(output).pages)
        self.assertIn("4 PEÇAS",text)
        self.assertIn("MARINHO",text)
        self.assertIn("30 un",text)
        self.assertNotIn("Estoque atual",text)
        self.assertNotIn("Saldo projetado",text)

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

    def test_cloud_download_rejects_unknown_columns_without_changing_local_data(self):
        product_id = self.create_product()
        sync = CloudSync(app.data_dir(), {})
        payload = sync.export_payload(self.db.db)
        payload["tables"]["products"][0]["unknown_column"] = "unsafe"
        snapshot = {"payload": payload, "revision": 2, "updated_at": "2026-08-26T10:00:00+00:00"}
        with self.assertRaisesRegex(CloudSyncError, "colunas inválidas"):
            sync._download_snapshot(self.db.db, snapshot)
        self.assertEqual(self.db.product(product_id)["name"], "MARINHO")

    def test_sku_mapping_remembers_multiple_products_and_normalizes_lookup(self):
        first_id = self.create_product(name="BOLSA", variant="Caramelo")
        self.db.save_product({"name":"MOCHILA","category":"Bolsa maternidade","group_name":"2 PEÇAS","variant":"Caramelo","unit":"un","minimum":0,"photo":"","notes":""})
        second_id = int(self.db.db.execute("SELECT MAX(id) id FROM products").fetchone()["id"])

        mapping_id = self.db.save_sku_mapping("  Caramelo   Leão 2P ", [first_id, second_id])

        self.assertEqual(self.db.sku_mapping_for("caramelo leao 2p")["id"], mapping_id)
        self.assertEqual({row["id"] for row in self.db.sku_mapping_products(mapping_id)}, {first_id, second_id})
        self.assertIn("sku_mappings", CloudSync(app.data_dir(), {}).export_payload(self.db.db)["tables"])

        summarized = self.db.sku_mappings("mochila")
        self.assertEqual(len(summarized), 1)
        self.assertIn("MOCHILA", summarized[0]["product_labels"])

    def test_mapped_sales_list_consolidates_products_shared_by_skus(self):
        first_id = self.create_product(name="BOLSA", variant="Caramelo")
        self.db.save_product({"name":"MOCHILA","category":"Bolsa maternidade","group_name":"2 PEÇAS","variant":"Caramelo","unit":"un","minimum":0,"photo":"","notes":""})
        second_id = int(self.db.db.execute("SELECT MAX(id) id FROM products").fetchone()["id"])
        self.db.save_sku_mapping("KIT A", [first_id, second_id])
        self.db.save_sku_mapping("KIT B", [first_id])

        from sales_list_import import SalesListItem
        review, draft = app.mapped_sales_list(self.db, [SalesListItem("KIT A", 2), SalesListItem("KIT B", 3)])

        self.assertEqual(len(review), 2)
        totals = {row["product_id"]: row["quantity"] for row in draft}
        self.assertEqual(totals, {first_id: 5, second_id: 2})


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

    def test_kit_conversion_page_and_mode_are_restored(self):
        path = self.folder / "ui-preferences.json"
        preferences = LocalPreferences(path)
        preferences.values.update({
            "last_page": "kit_conversion",
            "kit_conversion_mode": "Desmontagem",
            "kit_conversion_user": "Ana",
        })
        preferences.save()

        restored = LocalPreferences(path).values
        self.assertEqual(restored["last_page"], "kit_conversion")
        self.assertEqual(restored["kit_conversion_mode"], "Desmontagem")
        self.assertEqual(restored["kit_conversion_user"], "Ana")

    def test_movement_internal_page_is_restored(self):
        path = self.folder / "ui-preferences.json"
        preferences = LocalPreferences(path)
        preferences.values["movement_section"] = "history"
        preferences.save()
        self.assertEqual(LocalPreferences(path).values["movement_section"], "history")


class CountFlowTests(unittest.TestCase):
    def test_successful_count_reset_clears_product_search_and_restores_focus(self):
        calls = []

        class Value:
            def __init__(self):
                self.value = "FITA 5 PEÇAS • MARINHO [un]"

            def set(self, value):
                self.value = value

        class Entry:
            def focus_set(self):
                calls.append("focus")

        class CountScreen:
            c_selected_product_id = 27
            c_product = Value()
            c_product_entry = Entry()

            def hide_count_product_suggestions(self):
                calls.append("hide")

            def update_count_current(self):
                calls.append("balance")

            def after_idle(self, callback):
                calls.append("scheduled")
                callback()

        screen = CountScreen()
        app.EstoqueApp.reset_count_product_search(screen)

        self.assertIsNone(screen.c_selected_product_id)
        self.assertEqual(screen.c_product.value, "")
        self.assertEqual(calls, ["hide", "balance", "scheduled", "focus"])


class ScrollingTests(unittest.TestCase):
    def test_tree_wheel_moves_multiple_rows_and_canvas_keeps_fractional_impulse(self):
        self.assertEqual(tree_wheel_units(120), -3)
        self.assertEqual(tree_wheel_units(-240), 6)
        self.assertEqual(canvas_wheel_impulse(120), -84.0)
        self.assertEqual(canvas_wheel_impulse(-120), 84.0)


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
        tables = {name: [] for name in TABLES}
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


class BrandAssetTests(unittest.TestCase):
    def test_app_icon_uses_new_transparent_square_artwork(self):
        image = app_icon(64)
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertGreater(image.getpixel((32, 32))[2], image.getpixel((32, 32))[0])

    def test_company_logo_is_separate_from_application_icon(self):
        root = Path(__file__).resolve().parent
        with Image.open(root / "assets" / "brand" / "icone-bolsas-baby.png") as company_logo:
            self.assertEqual(company_logo.size, (1774, 887))
        with Image.open(root / "assets" / "brand" / "icone-aplicativo.png") as application_icon:
            self.assertEqual(application_icon.size, (1254, 1254))

    def test_native_window_icon_uses_packaged_multisize_ico(self):
        with Image.open(application_icon_path()) as native_icon:
            self.assertIn((16, 16), native_icon.info["sizes"])
            self.assertIn((20, 20), native_icon.info["sizes"])
            self.assertIn((24, 24), native_icon.info["sizes"])
            self.assertIn((32, 32), native_icon.info["sizes"])
            self.assertIn((40, 40), native_icon.info["sizes"])
            self.assertIn((256, 256), native_icon.info["sizes"])


if __name__ == "__main__":
    unittest.main()
