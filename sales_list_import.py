from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pdfplumber


class SalesListError(ValueError):
    """Raised when a PDF is not a supported sales list."""


@dataclass(frozen=True)
class SalesListItem:
    sku: str
    quantity: float


def normalize_sku_key(value: object) -> str:
    """Build a stable key while preserving the original SKU for display."""

    clean = " ".join(str(value or "").strip().split())
    decomposed = unicodedata.normalize("NFKD", clean.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _quantity(value: object) -> float:
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError as error:
        raise SalesListError(f"Quantidade inválida na lista: {value}") from error
    if number <= 0 or not math.isfinite(number):
        raise SalesListError(f"Quantidade inválida na lista: {value}")
    return number


def aggregate_items(items: list[SalesListItem]) -> list[SalesListItem]:
    totals: dict[str, float] = {}
    labels: dict[str, str] = {}
    order: list[str] = []
    for item in items:
        key = normalize_sku_key(item.sku)
        if not key:
            continue
        if key not in totals:
            totals[key] = 0.0
            labels[key] = " ".join(item.sku.strip().split())
            order.append(key)
        totals[key] += _quantity(item.quantity)
    return [SalesListItem(labels[key], totals[key]) for key in order]


def parse_shopee_words(words: list[dict], page_width: float, page_height: float) -> list[SalesListItem]:
    """Read quantity and SKU columns by their visual alignment on a Shopee page."""

    headers = []
    quantity_headers = [word for word in words if normalize_sku_key(word.get("text")) == "qnt"]
    sku_headers = [word for word in words if normalize_sku_key(word.get("text")) == "sku"]
    for quantity_header in quantity_headers:
        sku_header = min(
            (word for word in sku_headers if abs(float(word["top"]) - float(quantity_header["top"])) <= 4),
            key=lambda word: abs(float(word["top"]) - float(quantity_header["top"])),
            default=None,
        )
        if sku_header:
            headers.append((float(quantity_header["top"]), float(quantity_header["x0"]), float(sku_header["x0"])))
    headers.sort()

    results: list[SalesListItem] = []
    for header_index, (header_top, quantity_x, sku_x) in enumerate(headers):
        section_bottom = headers[header_index + 1][0] - 2 if header_index + 1 < len(headers) else page_height
        quantities = []
        for word in words:
            top = float(word["top"])
            if not header_top + 3 < top < section_bottom:
                continue
            if abs(float(word["x0"]) - quantity_x) > max(12, page_width * .025):
                continue
            if not re.fullmatch(r"\d+(?:[.,]\d+)?", str(word.get("text") or "").strip()):
                continue
            quantities.append(word)
        quantities.sort(key=lambda word: (float(word["top"]), float(word["x0"])))

        for index, quantity_word in enumerate(quantities):
            row_top = float(quantity_word["top"]) - 3
            row_bottom = (
                float(quantities[index + 1]["top"]) - 3
                if index + 1 < len(quantities)
                else section_bottom
            )
            sku_words = [
                word for word in words
                if row_top <= float(word["top"]) < row_bottom
                and float(word["x0"]) >= sku_x - 3
                and normalize_sku_key(word.get("text")) != "sku"
            ]
            sku_words.sort(key=lambda word: (round(float(word["top"]), 1), float(word["x0"])))
            sku = " ".join(str(word["text"]).strip() for word in sku_words if str(word.get("text") or "").strip())
            sku = " ".join(sku.split())
            if sku:
                results.append(SalesListItem(sku, _quantity(quantity_word["text"])))
    return results


def parse_mercado_livre_text(text: str) -> list[SalesListItem]:
    """Read each SKU/Quantidade block from a Mercado Livre list page."""

    pattern = re.compile(
        r"SKU\s*:\s*(?P<sku>[^\r\n]+)[\r\n]+[^\r\n]*?Quantidade\s*:\s*(?P<quantity>\d+(?:[.,]\d+)?)",
        re.IGNORECASE,
    )
    return [
        SalesListItem(" ".join(match.group("sku").split()), _quantity(match.group("quantity")))
        for match in pattern.finditer(text or "")
    ]


def _is_mercado_livre_list_page(text: str) -> bool:
    normalized = normalize_sku_key(text)
    return "produtos" in normalized and "sku:" in normalized and "quantidade:" in normalized


def read_sales_list(path: Path | str, source: str) -> list[SalesListItem]:
    pdf_path = Path(path)
    if pdf_path.suffix.lower() != ".pdf":
        raise SalesListError("Selecione um arquivo PDF.")
    if source not in ("shopee", "mercado_livre"):
        raise SalesListError("Tipo de lista não reconhecido.")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                raise SalesListError("O PDF está vazio.")
            if source == "shopee":
                parsed = []
                for page in pdf.pages:
                    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
                    parsed.extend(parse_shopee_words(words, float(page.width), float(page.height)))
            else:
                selected_pages = []
                for page in reversed(pdf.pages[-4:]):
                    text = page.extract_text() or ""
                    if _is_mercado_livre_list_page(text):
                        selected_pages.append(text)
                    elif selected_pages:
                        break
                parsed = []
                for text in reversed(selected_pages):
                    parsed.extend(parse_mercado_livre_text(text))
    except SalesListError:
        raise
    except Exception as error:
        raise SalesListError(f"Não foi possível ler o PDF: {error}") from error

    results = aggregate_items(parsed)
    if not results:
        label = "Shopee" if source == "shopee" else "Mercado Livre"
        raise SalesListError(f"Nenhum par de SKU e quantidade foi encontrado no formato {label}.")
    return results
