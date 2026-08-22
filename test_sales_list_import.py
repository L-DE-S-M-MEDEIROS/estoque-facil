from __future__ import annotations

import unittest

from sales_list_import import SalesListItem, aggregate_items, parse_mercado_livre_text, parse_shopee_words


def word(text, x, top):
    return {"text": text, "x0": x, "x1": x + len(text) * 5, "top": top, "bottom": top + 10}


class SalesListParserTests(unittest.TestCase):
    def test_shopee_uses_imaginary_quantity_to_sku_rows_and_joins_wrapped_sku(self):
        words = [
            word("Produto", 10, 10), word("Qnt", 100, 10), word("Variação", 160, 10), word("SKU", 300, 10),
            word("1", 100, 30), word("ignorado", 160, 30), word("caramelo", 300, 30), word("leão", 350, 30),
            word("2p", 300, 44),
            word("3", 100, 65), word("ignorado", 160, 65), word("marinho", 300, 65), word("2p", 355, 65),
        ]

        result = parse_shopee_words(words, 600, 800)

        self.assertEqual(result, [SalesListItem("caramelo leão 2p", 1), SalesListItem("marinho 2p", 3)])

    def test_mercado_livre_ignores_identification_product_and_buyer(self):
        text = """Identificação Produtos
ABC Produto que deve ser ignorado
Venda: 123 SKU:KIT-LEAO-4-P-BEGE
Nome do comprador Quantidade: 2
Cor: Bege
"""

        result = parse_mercado_livre_text(text)

        self.assertEqual(result, [SalesListItem("KIT-LEAO-4-P-BEGE", 2)])

    def test_repeated_sku_is_aggregated_ignoring_case_accents_and_extra_spaces(self):
        result = aggregate_items([SalesListItem("Saída  Verde", 2), SalesListItem("saida verde", 3)])
        self.assertEqual(result, [SalesListItem("Saída Verde", 5)])


if __name__ == "__main__":
    unittest.main()
