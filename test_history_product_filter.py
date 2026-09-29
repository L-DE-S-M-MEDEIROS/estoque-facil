from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app


class HistoryProductFilterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        folder = Path(self.temporary.name)
        (folder / "fotos").mkdir()
        self.folder_patch = patch.object(app, "data_dir", return_value=folder)
        self.folder_patch.start()
        self.addCleanup(self.folder_patch.stop)
        self.db = app.Database()
        self.addCleanup(self.db.db.close)
        self.target = self.product("MARINHO", "2 PEÇAS")
        self.other = self.product("MARINHO", "4 PEÇAS")

    def product(self, name, group):
        self.db.save_product(dict(name=name,group_name=group,variant="Azul",category="",unit="un",minimum=0,photo="",notes=""))
        return self.db.db.execute("SELECT id FROM products WHERE name=? AND group_name=?",(name,group)).fetchone()[0]

    def legacy(self, product_id, day="2026-09-01"):
        with self.db.db:
            cursor = self.db.db.execute("""INSERT INTO movements(product_id,type,quantity,resulting_stock,movement_date,reason,checked_by,created_at,operation_id)
                VALUES(?,'entrada',1,1,?,'Antigo','Teste',?,?)""",(product_id,day,day,self.db.operation("entrada")["id"]))
        return cursor.lastrowid

    def test_exact_product_keeps_whole_batch_and_legacy(self):
        legacy_id = self.legacy(self.target)
        self.legacy(self.other)
        batch = self.db.add_movement_batch("entrada",[(self.target,3),(self.other,4)],"2026-09-02","Conjunto","Teste")
        self.db.add_movement_batch("entrada",[(self.other,2)],"2026-09-03","Outro grupo","Teste")
        history = self.db.movement_history(product_id=self.target)
        self.assertEqual({row["history_key"] for row in history},{f"movement:{legacy_id}",f"batch:{batch}"})
        full_batch = next(row for row in history if row["batch_id"])
        self.assertEqual(full_batch["item_count"],2)
        self.assertIn("4 PEÇAS",full_batch["product_summary"])

    def test_product_combines_with_operation_and_inclusive_dates(self):
        self.db.add_movement_batch("entrada",[(self.target,10)],"2026-09-01","","Teste")
        sale = self.db.add_movement_batch("saida",[(self.target,2)],"2026-09-02","","Teste")
        result = self.db.movement_history(self.db.operation("saida")["id"],"2026-09-02","2026-09-02",self.target)
        self.assertEqual([row["history_key"] for row in result],[f"batch:{sale}"])
        self.assertEqual(self.db.movement_history(end_date="2026-08-31",product_id=self.target),[])
        self.assertEqual(self.db.movement_history(product_id=999999),[])

    def test_search_group_color_accents_and_only_products_with_history(self):
        self.legacy(self.target)
        self.legacy(self.other)
        self.product("SEM MOVIMENTAÇÃO", "2 PEÇAS")
        self.assertEqual([row["id"] for row in self.db.history_products("2 pec mar")],[self.target])
        self.assertEqual(len(self.db.history_products("azul")),2)
        self.assertEqual(len(self.db.history_products("mar")),2)
        self.assertEqual(self.db.history_products("inexistente"),[])
        self.assertEqual(self.db.history_products("sem movimentacao"),[])

    def test_selected_product_includes_history_beyond_default_500_limit(self):
        with self.db.db:
            self.db.db.executemany("""INSERT INTO movements(product_id,type,quantity,resulting_stock,movement_date,reason,checked_by,created_at)
                VALUES(?,'entrada',1,1,'2026-09-01','','Teste',?)""",[(self.target,str(i)) for i in range(501)])
        self.assertEqual(len(self.db.movement_history()),500)
        self.assertEqual(len(self.db.movement_history(product_id=self.target)),501)


if __name__ == "__main__":
    unittest.main()
