from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

import app
from premium_widgets import count_age_color


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


if __name__ == "__main__":
    unittest.main()
