"""Transient print selection: never changes stock or cloud state."""
from database_utils import normalize_identity_text


class StockPrintSelection:
    def __init__(self, products):
        self.products = sorted(products, key=lambda p: (
            normalize_identity_text(p["group_name"] or ""),
            normalize_identity_text(p["name"]),
            normalize_identity_text(p["variant"] or ""), int(p["id"]),
        ))
        self.groups = {}
        for product in self.products:
            group = str(product["group_name"] or "").strip()
            self.groups.setdefault(group, set()).add(int(product["id"]))
        self.ids = {int(p["id"]) for p in self.products}
        self.selected = set()

    def set_product(self, product_id, selected):
        if product_id not in self.ids:
            raise ValueError("Produto não está disponível para impressão.")
        if selected:self.selected.add(product_id)
        else:self.selected.discard(product_id)

    def set_group(self, group, selected):
        if selected:self.selected.update(self.groups[group])
        else:self.selected.difference_update(self.groups[group])

    def set_all(self, selected):
        self.selected = set(self.ids) if selected else set()

    def group_count(self, group):
        return len(self.groups[group] & self.selected)

    def selected_products(self):
        return [p for p in self.products if int(p["id"]) in self.selected]
