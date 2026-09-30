from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from pypdf import PdfReader
import app
from stock_print_selection import StockPrintSelection


def sample_products():
    return [dict(id=pid,group_name=group,name=name,variant="",stock=pid*10,unit="un")
            for pid,group,name in [(1,"2 PEÇAS","MARINHO"),(2,"2 PEÇAS","BEGE"),(3,"4 PEÇAS","MARINHO"),(4,"","VERDE")]]


class StockPrintSelectionTests(unittest.TestCase):
    def setUp(self):self.model=StockPrintSelection(sample_products())

    def test_individual_products_with_same_name_remain_separate(self):
        self.assertEqual(self.model.selected,set())
        self.model.set_product(1,True)
        self.assertEqual([p["id"] for p in self.model.selected_products()],[1])
        self.assertEqual(self.model.group_count("2 PEÇAS"),1)
        self.model.set_product(1,False)
        self.assertFalse(self.model.selected)

    def test_group_toggle_and_partial_selection(self):
        self.model.set_group("2 PEÇAS",True)
        self.model.set_group("4 PEÇAS",True)
        self.model.set_product(1,False)
        self.assertEqual(self.model.selected,{2,3})
        self.model.set_group("2 PEÇAS",False)
        self.assertEqual(self.model.selected,{3})
        self.model.set_group("",True)
        self.assertEqual(self.model.selected,{3,4})

    def test_all_clear_and_order(self):
        self.model.set_all(True)
        self.assertEqual(self.model.selected,{1,2,3,4})
        self.assertEqual([p["id"] for p in self.model.selected_products()],[4,2,1,3])
        self.model.set_all(False)
        self.assertFalse(self.model.selected_products())
        with self.assertRaises(ValueError):self.model.set_product(999,True)

    def test_pdf_contains_only_selected_products(self):
        self.model.set_product(2,True)
        self.model.set_product(3,True)
        with tempfile.TemporaryDirectory() as folder:
            output=app.build_current_stock_print_pdf(Path(folder)/"selected.pdf",self.model.selected_products())
            text="\n".join(p.extract_text() for p in PdfReader(output).pages)
        self.assertIn("BEGE",text);self.assertIn("MARINHO",text)
        self.assertIn("Conferência",text);self.assertIn("20 un",text);self.assertIn("30 un",text)
        self.assertNotIn("VERDE",text);self.assertNotIn("10 un",text)

    def test_cancel_does_not_generate_or_open_pdf(self):
        gui=SimpleNamespace(db=SimpleNamespace(stock_products=Mock(return_value=sample_products())),wait_window=Mock())
        with patch.object(app,"StockPrintSelectionDialog",return_value=SimpleNamespace(result=None)),patch.object(app,"build_current_stock_print_pdf") as build,patch.object(app.os,"startfile") as open_file:
            app.EstoqueApp.print_current_stock(gui)
        build.assert_not_called();open_file.assert_not_called()

    def test_print_refreshes_balance_and_passes_only_selected_ids(self):
        products=sample_products();fresh=sample_products();fresh[0]["stock"]=999
        gui=SimpleNamespace(db=SimpleNamespace(stock_products=Mock(side_effect=[products,fresh])),wait_window=Mock())
        with tempfile.TemporaryDirectory() as folder,patch.object(app,"data_dir",return_value=Path(folder)),patch.object(app,"StockPrintSelectionDialog",return_value=SimpleNamespace(result={1})),patch.object(app,"build_current_stock_print_pdf") as build,patch.object(app.os,"startfile") as open_file:
            app.EstoqueApp.print_current_stock(gui)
        rows=build.call_args.args[1]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]["stock"],999)
        open_file.assert_called_once()


if __name__=="__main__":unittest.main()
