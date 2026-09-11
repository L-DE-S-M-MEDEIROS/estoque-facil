from datetime import date
import unittest
import test_app
import app
from unittest.mock import patch, Mock
from types import SimpleNamespace


class TestProductTests(unittest.TestCase):
    setUp = test_app.InventoryDatabaseTests.setUp
    tearDown = test_app.InventoryDatabaseTests.tearDown
    create_product = test_app.InventoryDatabaseTests.create_product
    def test_untracked_product_keeps_movements_but_not_stock_reports(self):
        pid = self.create_product(name='TESTE', group='', variant='')
        self.db.add_movement_batch('saida', [(pid, 10000)], date.today().isoformat(), 'Teste', 'Teste')
        self.assertEqual(self.db.product(pid)['name'], 'TESTE')
        self.assertTrue(self.db.products('teste'))
        self.assertEqual(self.db.stock_products(), [])
        self.assertEqual(self.db.negative_stock_products(), [])
        self.assertEqual(self.db.monthly_stock_rows(date.today().strftime('%Y-%m')), [])
        self.assertEqual(app.product_stock_label(self.db.product(pid)), '∞ (sem controle)')
        self.assertEqual(self.db.stock(pid), -10000)

    def test_only_exact_test_name_without_group_is_untracked(self):
        self.assertFalse(app.is_test_product({'name': 'TESTE', 'group_name': 'Casinha'}))
        self.assertFalse(app.is_test_product({'name': 'Bolsa teste', 'group_name': ''}))
        self.assertTrue(app.is_test_product({'name': ' teste ', 'group_name': ' '}))

    def test_stock_screen_omits_test_product(self):
        self.create_product(name='TESTE', group='', variant='')
        tree = Mock()
        tree.get_children.return_value = ()
        alert = Mock()
        alert.winfo_manager.return_value = ''
        screen = SimpleNamespace(db=self.db, stock_tree=tree,
            negative_stock_alert=alert, stock_confidence_cells=Mock(),
            stock_quantity_cells=Mock(), stock_cards=[Mock() for _ in range(4)])
        app.EstoqueApp.refresh_stock(screen)
        tree.insert.assert_not_called()
        screen.stock_cards[0].configure.assert_called_once_with(text='0')

    def test_same_clock_tick_keeps_count_correction_order(self):
        pid = self.create_product()
        day = date.today().isoformat()
        with patch.object(app, 'datetime', wraps=app.datetime) as clock:
            clock.now.return_value = app.datetime(2026, 9, 11, 12, 0)
            self.db.add_movement(pid, 'inventario', 12, day, 'Inicial', 'Teste')
            self.db.save_monthly_count(pid, day[:7], 10, 'Teste', day)
            self.db.add_movement(pid, 'entrada', 3, day, 'Entrada', 'Teste')
            self.db.save_monthly_count(pid, day[:7], 11, 'Teste', day)
        self.assertEqual(self.db.stock(pid), 14)
