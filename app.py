from __future__ import annotations

import ctypes
import math
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import ImageTk
from reportlab.lib import colors as pdf_colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape as xml_escape

from premium_icons import app_icon, application_icon_path, brand_mark, icon
from premium_widgets import MaskedDateEntry, SmoothScrollableFrame, TreeConfidenceOverlay, TreeRelativeDateOverlay, TreeRowSeparatorOverlay, TreeStockOverlay, confidence_tier, tree_wheel_units
from cloud_sync import CloudSync, CloudSyncError
from database_utils import configure_database_connection, database_integrity_errors, normalize_identity_text
from local_state import LocalCloudSession, LocalPreferences, LocalSimulationDraft, read_json_object
from sales_list_import import SalesListError, normalize_sku_key, read_sales_list
from updater import UpdateError, check_for_update, download_update, run_update_helper, schedule_update_cleanup, start_update_install

APP_NAME = "ESTOQUE BOLSAS BABY"
APP_VERSION = "1.2.6"
GITHUB_REPO = "L-DE-S-M-MEDEIROS/estoque-facil"
SEARCH_RESULT_LIMIT = 18

KIT_PIECE_COUNTS = (2, 4, 5)
KIT_INTERNAL_OPERATIONS = {
    "montagem": "kit_assembly",
    "desmembramento": "kit_disassembly",
}

QUICK_STOCK_ACTIONS = {
    "Defeito": ("DEFEITO", "negative", -1),
    "Devolução": ("DEVOLUÇÃO", "positive", 1),
}

COLORS = {
    "background": ("#F6F7F9", "#0B0F16"),
    "surface": ("#FFFFFF", "#121824"),
    "surface_hover": ("#F0F4F8", "#192232"),
    "surface_alt": ("#EEF3F8", "#171E2B"),
    "text": ("#202936", "#F3F7FB"),
    "muted": ("#748092", "#91A0B5"),
    "border": ("#DEE5EC", "#263244"),
    "accent": ("#2B6F9F", "#36BFFA"),
    "accent_hover": ("#245F89", "#67D3FF"),
    "accent_soft": ("#E4F0F7", "#102B3D"),
    "nav_selected": ("#D1E7F3", "#102B3D"),
    "danger": ("#C75353", "#FF7B7B"),
    "warning": ("#C47B32", "#FFB768"),
    "success": ("#2E8B68", "#4DD6A3"),
    "sidebar": ("#EDF3F8", "#0F1520"),
}


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class _Rect(ctypes.Structure):
    _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long))


class _MonitorInfo(ctypes.Structure):
    _fields_ = (("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", ctypes.c_ulong))


def monitor_work_areas(screen_width: int, screen_height: int) -> list[tuple[int, int, int, int]]:
    """Return monitor work areas, keeping the primary monitor first."""
    if sys.platform != "win32":
        return [(0, 0, screen_width, screen_height)]

    monitors: list[tuple[bool, tuple[int, int, int, int]]] = []
    try:
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_Rect),
            ctypes.c_ssize_t,
        )

        def collect(handle, _device_context, _monitor_rect, _data):
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(_MonitorInfo)
            if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                work = info.rcWork
                monitors.append((bool(info.dwFlags & 1), (work.left, work.top, work.right, work.bottom)))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except (AttributeError, OSError, ValueError):
        monitors = []

    if not monitors:
        return [(0, 0, screen_width, screen_height)]
    monitors.sort(key=lambda item: not item[0])
    return [area for _primary, area in monitors]


def parse_window_geometry(value: object) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", str(value or ""))
    if not match:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    return width, height, x, y


def visible_window_geometry(
    saved: object,
    work_areas: list[tuple[int, int, int, int]],
    minimum_width: int,
    minimum_height: int,
) -> str:
    """Clamp a saved window to a currently connected monitor's work area."""
    primary = work_areas[0]
    parsed = parse_window_geometry(saved)
    if parsed is None:
        left, top, right, bottom = primary
        width = max(minimum_width, round((right - left) * .90))
        height = max(minimum_height, round((bottom - top) * .88))
        width, height = min(width, right - left), min(height, bottom - top)
        x, y = left + (right - left - width) // 2, top + (bottom - top - height) // 2
        return f"{width}x{height}+{x}+{y}"

    width, height, x, y = parsed

    def overlap(area: tuple[int, int, int, int]) -> int:
        left, top, right, bottom = area
        return max(0, min(x + width, right) - max(x, left)) * max(0, min(y + height, bottom) - max(y, top))

    target = max(work_areas, key=overlap)
    if overlap(target) == 0:
        target = primary
    left, top, right, bottom = target
    width = min(max(width, minimum_width), right - left)
    height = min(max(height, minimum_height), bottom - top)
    x = min(max(x, left), right - width)
    y = min(max(y, top), bottom - height)
    return f"{width}x{height}{x:+d}{y:+d}"


def centered_dialog_geometry(
    parent: tk.Misc,
    desired_width: int,
    desired_height: int,
    margin: int = 24,
) -> str:
    """Fit and visually center a CustomTkinter dialog on its parent monitor.

    CustomTkinter scales the requested width and height, but deliberately keeps
    the x/y coordinates unscaled.  Monitor work areas are physical pixels, so
    both spaces must be reconciled before calculating the centered position.
    """

    fallback = [(0, 0, parent.winfo_screenwidth(), parent.winfo_screenheight())]
    work_areas = getattr(parent, "work_areas", fallback) or fallback
    try:
        parent.update_idletasks()
        center_x = parent.winfo_rootx() + max(1, parent.winfo_width()) // 2
        center_y = parent.winfo_rooty() + max(1, parent.winfo_height()) // 2
    except tk.TclError:
        center_x = (work_areas[0][0] + work_areas[0][2]) // 2
        center_y = (work_areas[0][1] + work_areas[0][3]) // 2
    target = next(
        (area for area in work_areas if area[0] <= center_x < area[2] and area[1] <= center_y < area[3]),
        work_areas[0],
    )
    left, top, right, bottom = target
    try:
        window_scaling = float(ctk.ScalingTracker.get_window_scaling(parent))
    except (AttributeError, KeyError, TypeError, ValueError):
        window_scaling = 1.0
    window_scaling = max(0.5, min(window_scaling, 4.0))
    # Tk's geometry describes the client area. Reserve the native Windows frame,
    # title bar and a small safety strip so the bottom actions never sit behind
    # the taskbar on short displays.
    available_width = max(320, right - left - margin * 2 - 16)
    available_height = max(280, bottom - top - margin * 2 - 136)
    physical_width = min(max(round(320 * window_scaling), round(desired_width * window_scaling)), available_width)
    physical_height = min(max(round(280 * window_scaling), round(desired_height * window_scaling)), available_height)
    width = max(1, round(physical_width / window_scaling))
    height = max(1, round(physical_height / window_scaling))
    physical_width = min(available_width, round(width * window_scaling))
    physical_height = min(available_height, round(height * window_scaling))
    x = left + (right - left - physical_width) // 2
    y = top + (bottom - top - physical_height) // 2
    return f"{width}x{height}{x:+d}{y:+d}"


def centered_outer_position(
    work_area: tuple[int, int, int, int],
    outer_width: int,
    outer_height: int,
) -> tuple[int, int]:
    """Return the exact position that centers an outer window rectangle."""

    left, top, right, bottom = work_area
    return (
        left + ((right - left) - outer_width) // 2,
        top + ((bottom - top) - outer_height) // 2,
    )


def center_native_window(window: tk.Misc, parent: tk.Misc) -> None:
    """Center the complete native frame on the monitor containing ``parent``."""

    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        user32 = ctypes.windll.user32
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        get_ancestor.restype = ctypes.c_void_p
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        monitor_from_window.restype = ctypes.c_void_p

        window_handle = get_ancestor(ctypes.c_void_p(window.winfo_id()), 2)
        parent_handle = get_ancestor(ctypes.c_void_p(parent.winfo_id()), 2)
        if not window_handle or not parent_handle:
            return
        rectangle = _Rect()
        if not user32.GetWindowRect(ctypes.c_void_p(window_handle), ctypes.byref(rectangle)):
            return
        monitor = monitor_from_window(ctypes.c_void_p(parent_handle), 2)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not monitor or not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return
        work = info.rcWork
        x, y = centered_outer_position(
            (work.left, work.top, work.right, work.bottom),
            rectangle.right - rectangle.left,
            rectangle.bottom - rectangle.top,
        )
        flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
        user32.SetWindowPos(ctypes.c_void_p(window_handle), None, x, y, 0, 0, flags)
    except (AttributeError, OSError, tk.TclError, ValueError):
        return


def data_dir() -> Path:
    folder = Path.home() / "AppData" / "Local" / "EstoqueFacil"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fotos").mkdir(exist_ok=True)
    return folder


def fmt_number(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def normalize_search_text(value: object) -> str:
    """Normalize case and accents so partial searches stay intuitive."""
    return normalize_identity_text(value)


def product_matches_search(product: sqlite3.Row, query: str) -> bool:
    """Match a product by partial name, group, variation or category."""
    needle = normalize_search_text(query).strip()
    if not needle:
        return True
    searchable = " ".join(
        str(product[field] or "")
        for field in ("name", "group_name", "variant", "category")
    )
    haystack = normalize_search_text(searchable)
    tokens = re.findall(r"[a-z0-9]+", needle)
    return needle in haystack or (bool(tokens) and all(token in haystack for token in tokens))


def relative_past_date(value: str, today: date | None = None) -> str:
    """Turn an ISO date into a concise Portuguese relative date."""
    if not value:
        return "Nunca contado"
    reference = today or date.today()
    counted = datetime.strptime(value, "%Y-%m-%d").date()
    days = max(0, (reference - counted).days)
    if days == 0:
        return "Hoje"
    if days == 1:
        return "Ontem"
    if days < 7:
        return f"{days} dias atrás"
    if days < 30:
        weeks = days // 7
        return f"{weeks} semana atrás" if weeks == 1 else f"{weeks} semanas atrás"
    if days < 365:
        months = days // 30
        return f"{months} mês atrás" if months == 1 else f"{months} meses atrás"
    years = days // 365
    return f"{years} ano atrás" if years == 1 else f"{years} anos atrás"


def product_label(product: sqlite3.Row) -> str:
    parts = [product["group_name"], product["name"], product["variant"]]
    return " • ".join(str(part) for part in parts if part)


def kit_piece_count(product_or_group: sqlite3.Row | dict | str) -> int | None:
    """Return the supported kit size encoded in a product group."""
    if isinstance(product_or_group, str):
        group_name = product_or_group
    else:
        group_name = str(product_or_group["group_name"] or "")
    match = re.search(r"\b(2|4|5)\s*pecas\b", normalize_identity_text(group_name))
    return int(match.group(1)) if match else None


def kit_group_family(product_or_group: sqlite3.Row | dict | str) -> str:
    """Normalize the portion of a kit group that is not its piece count."""
    if isinstance(product_or_group, str):
        group_name = product_or_group
    else:
        group_name = str(product_or_group["group_name"] or "")
    normalized = normalize_identity_text(group_name)
    without_size = re.sub(r"\b(?:2|4|5)\s*pecas\b", " ", normalized)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_size).split())


def kit_variation_key(product: sqlite3.Row | dict) -> tuple[str, str, str]:
    """Identify the exact color/variation that must be preserved in a conversion."""
    return (
        kit_group_family(product),
        normalize_identity_text(product["name"]),
        normalize_identity_text(product["variant"]),
    )


def compatible_smaller_kits(selected: sqlite3.Row | dict, products) -> list:
    """List only smaller supported kits from the exact same family and variation."""
    selected_count = kit_piece_count(selected)
    if selected_count not in (4, 5):
        return []
    identity = kit_variation_key(selected)
    matches = [
        product
        for product in products
        if int(product["id"]) != int(selected["id"])
        and kit_piece_count(product) in KIT_PIECE_COUNTS
        and kit_piece_count(product) < selected_count
        and kit_variation_key(product) == identity
    ]
    return sorted(matches, key=lambda product: (kit_piece_count(product), product_label(product)))


def simulated_stock(current_stock: float, quantity: float, operation: str) -> float:
    """Calculate a projection without changing inventory data."""

    current = float(current_stock)
    amount = float(quantity)
    if operation not in ("entrada", "saida"):
        raise ValueError("Operação de simulação inválida.")
    if amount <= 0 or not math.isfinite(amount):
        raise ValueError("A quantidade deve ser maior que zero.")
    return current + amount if operation == "entrada" else current - amount


def simulation_stock_comparison(products, items, operation: str) -> list[dict]:
    """Build a complete before-and-after stock position for a simulation."""

    if operation not in ("entrada", "saida"):
        raise ValueError("Operação de simulação inválida.")
    quantities = {int(item["product_id"]): float(item["quantity"]) for item in items}
    comparison = []
    for product in products:
        product_id = int(product["id"])
        current = float(product["stock"])
        quantity = quantities.get(product_id)
        projected = current if quantity is None else simulated_stock(current, quantity, operation)
        comparison.append(
            {
                "product": product,
                "product_id": product_id,
                "current": current,
                "quantity": quantity,
                "projected": projected,
            }
        )
    return comparison


def simulation_selected_rows(products, items, operation: str) -> list[dict]:
    """Return only products that belong to the current simulation set."""

    return [
        row
        for row in simulation_stock_comparison(products, items, operation)
        if row["quantity"] is not None
    ]


def build_simulation_print_pdf(output_path: Path, rows: list[dict], operation: str, generated_at: datetime | None = None) -> Path:
    """Create a separation list with space for a manual physical check."""

    if not rows:
        raise ValueError("Adicione produtos à simulação antes de imprimir.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at or datetime.now()
    operation_label = "Entrada" if operation == "entrada" else "Saída"
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("SimulationTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=pdf_colors.HexColor("#202936"), alignment=TA_LEFT, spaceAfter=5*mm)
    meta_style = ParagraphStyle("SimulationMeta", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=12, textColor=pdf_colors.HexColor("#657386"), alignment=TA_LEFT)
    cell_style = ParagraphStyle("SimulationCell", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=12, textColor=pdf_colors.HexColor("#202936"), alignment=TA_LEFT)
    quantity_style = ParagraphStyle("SimulationQuantity", parent=cell_style, alignment=TA_CENTER)
    document = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=16*mm, leftMargin=16*mm, topMargin=16*mm, bottomMargin=17*mm, title="Lista de separação - Simulação", author=APP_NAME)
    story = [
        Paragraph("Lista de separação - Simulação", title_style),
        Paragraph(f"Operação simulada: {operation_label} &nbsp;&nbsp;|&nbsp;&nbsp; {len(rows)} produto(s) &nbsp;&nbsp;|&nbsp;&nbsp; Gerada em {timestamp.strftime('%d/%m/%Y às %H:%M')}", meta_style),
        Spacer(1, 2.5*mm),
        Paragraph("Anote na coluna <b>Conferência</b> a quantidade separada fisicamente.", meta_style),
        Spacer(1, 6*mm),
    ]
    table_data = [[
        Paragraph("Produto", cell_style),
        Paragraph("Quantidade simulada", quantity_style),
        Paragraph("Conferência", quantity_style),
    ]]
    for row in rows:
        product = row["product"]
        confirmation_box = Table([[""]], colWidths=[27*mm], rowHeights=[7*mm])
        confirmation_box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, pdf_colors.HexColor("#7C8EA1")),
            ("BACKGROUND", (0, 0), (-1, -1), pdf_colors.white),
        ]))
        table_data.append([
            Paragraph(xml_escape(product_label(product)), cell_style),
            Paragraph(f"{fmt_number(row['quantity'])} {xml_escape(str(product['unit']))}", quantity_style),
            confirmation_box,
        ])
    table = Table(table_data, colWidths=[104*mm, 34*mm, 37*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), pdf_colors.HexColor("#E4F0F7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), pdf_colors.HexColor("#245F89")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, pdf_colors.HexColor("#D5DEE7")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [pdf_colors.white, pdf_colors.HexColor("#F8FAFC")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)

    def draw_footer(canvas, doc):
        canvas.saveState();canvas.setStrokeColor(pdf_colors.HexColor("#D5DEE7"));canvas.line(16*mm,12*mm,A4[0]-16*mm,12*mm);canvas.setFont("Helvetica",8);canvas.setFillColor(pdf_colors.HexColor("#748092"));canvas.drawString(16*mm,7.5*mm,APP_NAME);canvas.drawRightString(A4[0]-16*mm,7.5*mm,f"Página {doc.page}");canvas.restoreState()

    document.build(story,onFirstPage=draw_footer,onLaterPages=draw_footer)
    return output


def build_current_stock_print_pdf(output_path: Path, products, generated_at: datetime | None = None) -> Path:
    """Create the complete stock-count sheet with a blank manual check field."""

    product_rows = list(products)
    if not product_rows:
        raise ValueError("Cadastre produtos antes de imprimir o estoque atual.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at or datetime.now()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CurrentStockTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, textColor=pdf_colors.HexColor("#202936"),
        alignment=TA_LEFT, spaceAfter=5*mm,
    )
    meta_style = ParagraphStyle(
        "CurrentStockMeta", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=9, leading=12, textColor=pdf_colors.HexColor("#657386"), alignment=TA_LEFT,
    )
    cell_style = ParagraphStyle(
        "CurrentStockCell", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=9.2, leading=11.5, textColor=pdf_colors.HexColor("#202936"), alignment=TA_LEFT,
    )
    centered_style = ParagraphStyle("CurrentStockCentered", parent=cell_style, alignment=TA_CENTER)
    document = SimpleDocTemplate(
        str(output), pagesize=A4, rightMargin=16*mm, leftMargin=16*mm,
        topMargin=16*mm, bottomMargin=17*mm,
        title="Estoque atual - Folha de conferência", author=APP_NAME,
    )
    story = [
        Paragraph("Estoque atual - Folha de conferência", title_style),
        Paragraph(
            f"{len(product_rows)} produto(s) &nbsp;&nbsp;|&nbsp;&nbsp; Gerada em {timestamp.strftime('%d/%m/%Y às %H:%M')}",
            meta_style,
        ),
        Spacer(1, 2.5*mm),
        Paragraph("Anote na coluna <b>Conferência</b> a quantidade encontrada na contagem física.", meta_style),
        Spacer(1, 6*mm),
    ]
    table_data = [[
        Paragraph("Grupo", cell_style),
        Paragraph("Produto", cell_style),
        Paragraph("Saldo atual", centered_style),
        Paragraph("Conferência", centered_style),
    ]]
    for product in product_rows:
        group_name = str(product["group_name"] or "Sem grupo")
        product_name = str(product["name"] or "")
        if product["variant"]:
            product_name = f"{product_name} • {product['variant']}"
        confirmation_box = Table([[""]], colWidths=[27*mm], rowHeights=[7*mm])
        confirmation_box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, pdf_colors.HexColor("#7C8EA1")),
            ("BACKGROUND", (0, 0), (-1, -1), pdf_colors.white),
        ]))
        table_data.append([
            Paragraph(xml_escape(group_name), cell_style),
            Paragraph(xml_escape(product_name), cell_style),
            Paragraph(f"{fmt_number(product['stock'])} {xml_escape(str(product['unit']))}", centered_style),
            confirmation_box,
        ])
    table = Table(table_data, colWidths=[42*mm, 72*mm, 30*mm, 31*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), pdf_colors.HexColor("#E4F0F7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), pdf_colors.HexColor("#245F89")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, pdf_colors.HexColor("#D5DEE7")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [pdf_colors.white, pdf_colors.HexColor("#F8FAFC")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)

    def draw_footer(canvas, doc):
        canvas.saveState();canvas.setStrokeColor(pdf_colors.HexColor("#D5DEE7"));canvas.line(16*mm,12*mm,A4[0]-16*mm,12*mm);canvas.setFont("Helvetica",8);canvas.setFillColor(pdf_colors.HexColor("#748092"));canvas.drawString(16*mm,7.5*mm,APP_NAME);canvas.drawRightString(A4[0]-16*mm,7.5*mm,f"Página {doc.page}");canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return output


def mapped_sales_list(database, items) -> tuple[list[dict], list[dict]]:
    """Resolve imported SKUs and consolidate the resulting stock draft."""

    review_rows = []
    totals: dict[int, dict] = {}
    for item in items:
        mapping = database.sku_mapping_for(item.sku)
        products = database.sku_mapping_products(int(mapping["id"])) if mapping else []
        if not products:
            raise ValueError(f"O SKU “{item.sku}” ainda não possui produtos vinculados.")
        product_names = []
        for product in products:
            product_id = int(product["id"])
            amount = float(item.quantity) * float(product["quantity_per_sale"])
            product_names.append(product_label(product))
            if product_id not in totals:
                totals[product_id] = {"product_id": product_id, "quantity": 0.0, "product": product}
            totals[product_id]["quantity"] += amount
        review_rows.append({
            "sku": item.sku,
            "quantity": float(item.quantity),
            "mapping_id": int(mapping["id"]),
            "products": product_names,
        })
    return review_rows, list(totals.values())


class Database:
    def __init__(self) -> None:
        self.path = data_dir() / "estoque.db"
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        configure_database_connection(self.db)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'un',
                cost REAL, minimum REAL NOT NULL DEFAULT 0, photo TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_types(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                effect TEXT NOT NULL CHECK(effect IN ('positive','negative','set')),
                legacy_type TEXT UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                protected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS product_groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS movement_batches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id INTEGER NOT NULL,
                movement_date TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                performed_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(operation_id) REFERENCES operation_types(id));
            CREATE TABLE IF NOT EXISTS movements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('entrada','saida','ajuste','inventario')),
                quantity REAL NOT NULL, resulting_stock REAL NOT NULL, informed_quantity REAL,
                movement_date TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                checked_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                operation_id INTEGER, batch_id INTEGER,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT);
            CREATE TABLE IF NOT EXISTS sku_mappings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                normalized_sku TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sku_mapping_products(
                mapping_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity_per_sale REAL NOT NULL DEFAULT 1 CHECK(quantity_per_sale>0),
                PRIMARY KEY(mapping_id,product_id),
                FOREIGN KEY(mapping_id) REFERENCES sku_mappings(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id);
            CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(movement_date DESC);
            CREATE INDEX IF NOT EXISTS idx_sku_mapping_products_product ON sku_mapping_products(product_id);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(products)")}
        if "group_name" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        if "variant" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
        self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS prevent_duplicate_product_insert
            BEFORE INSERT ON products
            WHEN EXISTS(
                SELECT 1 FROM products current
                WHERE normalize_identity_text(current.name)=normalize_identity_text(NEW.name)
                  AND normalize_identity_text(current.group_name)=normalize_identity_text(NEW.group_name)
            )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate_product_same_group');
            END;
            CREATE TRIGGER IF NOT EXISTS prevent_duplicate_product_update
            BEFORE UPDATE OF name,group_name ON products
            WHEN EXISTS(
                SELECT 1 FROM products current
                WHERE current.id<>NEW.id
                  AND normalize_identity_text(current.name)=normalize_identity_text(NEW.name)
                  AND normalize_identity_text(current.group_name)=normalize_identity_text(NEW.group_name)
            )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate_product_same_group');
            END;
        """)
        self.db.execute("""INSERT OR IGNORE INTO product_groups(name,active,created_at)
            SELECT DISTINCT TRIM(group_name),1,? FROM products WHERE TRIM(COALESCE(group_name,''))<>''""", (datetime.now().isoformat(timespec="seconds"),))
        movement_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(movements)")}
        if "checked_by" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN checked_by TEXT NOT NULL DEFAULT ''")
        if "informed_quantity" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN informed_quantity REAL")
            self.db.execute("UPDATE movements SET informed_quantity=resulting_stock WHERE type IN ('ajuste','inventario')")
        if "operation_id" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN operation_id INTEGER REFERENCES operation_types(id)")
        if "batch_id" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN batch_id INTEGER REFERENCES movement_batches(id)")
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_movement_batches_operation ON movement_batches(operation_id);
            CREATE INDEX IF NOT EXISTS idx_movements_batch ON movements(batch_id);
            CREATE INDEX IF NOT EXISTS idx_movements_operation ON movements(operation_id);
            CREATE INDEX IF NOT EXISTS idx_movements_product_timeline
                ON movements(product_id,movement_date,created_at,id);
        """)
        created_at = datetime.now().isoformat(timespec="seconds")
        for name, effect, legacy_type in (
            ("Entrada", "positive", "entrada"),
            ("Saída", "negative", "saida"),
            ("Ajuste", "set", "ajuste"),
            ("Inventário", "set", "inventario"),
            ("Montagem de kits", "negative", KIT_INTERNAL_OPERATIONS["montagem"]),
            ("Desmontagem de kits", "negative", KIT_INTERNAL_OPERATIONS["desmembramento"]),
        ):
            self.db.execute("""INSERT OR IGNORE INTO operation_types(name,effect,legacy_type,active,protected,created_at)
                VALUES(?,?,?,1,1,?)""", (name, effect, legacy_type, created_at))
        self.db.execute("""UPDATE movements SET operation_id=(
            SELECT id FROM operation_types WHERE legacy_type=movements.type
        ) WHERE operation_id IS NULL""")
        self.db.execute("""UPDATE OR IGNORE operation_types SET name='Desmontagem de kits'
            WHERE legacy_type='kit_disassembly' AND name<>'Desmontagem de kits'""")
        self.db.commit()
        self.on_change = None
        self._products_cache: tuple[sqlite3.Row, ...] | None = None
        self._products_cache_data_version: int | None = None
        self._products_by_id: dict[int, sqlite3.Row] = {}
        self.db.set_trace_callback(self._track_change)

    def _track_change(self, statement: str) -> None:
        if not re.match(r"^\s*(INSERT|UPDATE|DELETE)\b", statement, re.IGNORECASE):
            return
        self.invalidate_caches()
        if self.on_change:
            self.on_change()

    def invalidate_caches(self) -> None:
        self._products_cache = None
        self._products_cache_data_version = None
        self._products_by_id = {}

    def _database_data_version(self) -> int:
        return int(self.db.execute("PRAGMA data_version").fetchone()[0])

    def _cached_products(self) -> tuple[sqlite3.Row, ...]:
        data_version = self._database_data_version()
        if self._products_cache is None or self._products_cache_data_version != data_version:
            rows = self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
                FROM products p LEFT JOIN movements m ON m.product_id=p.id
                GROUP BY p.id ORDER BY
                    CASE WHEN TRIM(COALESCE(p.group_name,''))='' THEN 1 ELSE 0 END,
                    p.group_name COLLATE NOCASE,p.name COLLATE NOCASE,p.variant COLLATE NOCASE""").fetchall()
            self._products_cache = tuple(rows)
            self._products_by_id = {int(row["id"]): row for row in rows}
            self._products_cache_data_version = data_version
        return self._products_cache

    def ensure_kit_operations(self) -> bool:
        """Restore internal conversion operations after importing an older cloud snapshot."""
        existing = {
            str(row["legacy_type"])
            for row in self.db.execute("SELECT legacy_type FROM operation_types WHERE legacy_type IN ('kit_assembly','kit_disassembly')")
        }
        missing = [
            ("Montagem de kits", "negative", KIT_INTERNAL_OPERATIONS["montagem"]),
            ("Desmontagem de kits", "negative", KIT_INTERNAL_OPERATIONS["desmembramento"]),
        ]
        missing = [definition for definition in missing if definition[2] not in existing]
        disassembly = self.operation(KIT_INTERNAL_OPERATIONS["desmembramento"])
        can_rename = bool(
            disassembly
            and disassembly["name"] != "Desmontagem de kits"
            and not self.db.execute("SELECT 1 FROM operation_types WHERE name='Desmontagem de kits' COLLATE NOCASE").fetchone()
        )
        if not missing and not can_rename:
            return False
        created_at = datetime.now().isoformat(timespec="seconds")
        with self.db:
            if missing:
                self.db.executemany("""INSERT INTO operation_types(name,effect,legacy_type,active,protected,created_at)
                    VALUES(?,?,?,1,1,?)""", ((name, effect, legacy_type, created_at) for name, effect, legacy_type in missing))
            if can_rename:
                self.db.execute("UPDATE operation_types SET name='Desmontagem de kits' WHERE legacy_type='kit_disassembly'")
        return True

    def operations(self, include_inactive: bool = False, custom_only: bool = False, include_internal: bool = False) -> list[sqlite3.Row]:
        conditions = [] if include_internal else ["COALESCE(legacy_type,'') NOT IN ('kit_assembly','kit_disassembly')"]
        if not include_inactive:
            conditions.append("active=1")
        if custom_only:
            conditions.append("protected=0")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.db.execute(f"""SELECT * FROM operation_types {where} ORDER BY
            protected DESC,
            CASE legacy_type WHEN 'entrada' THEN 1 WHEN 'saida' THEN 2 WHEN 'ajuste' THEN 3 WHEN 'inventario' THEN 4 ELSE 5 END,
            name COLLATE NOCASE""").fetchall()

    def operation(self, operation: int | str) -> sqlite3.Row | None:
        if isinstance(operation, int) or str(operation).isdigit():
            return self.db.execute("SELECT * FROM operation_types WHERE id=?", (int(operation),)).fetchone()
        return self.db.execute("SELECT * FROM operation_types WHERE legacy_type=? OR name=? COLLATE NOCASE", (str(operation), str(operation))).fetchone()

    def save_operation(self, name: str, effect: str, operation_id: int | None = None) -> int:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Informe o nome da operação.")
        if len(clean_name) > 40:
            raise ValueError("O nome da operação pode ter no máximo 40 caracteres.")
        if effect not in ("positive", "negative", "set"):
            raise ValueError("Escolha se a operação soma ou retira do estoque.")
        try:
            with self.db:
                if operation_id:
                    current = self.operation(operation_id)
                    if not current:
                        raise ValueError("Essa operação não existe mais.")
                    saved_effect = current["effect"] if current["protected"] else effect
                    self.db.execute("UPDATE operation_types SET name=?,effect=?,active=1 WHERE id=?", (clean_name, saved_effect, operation_id))
                    return operation_id
                if effect == "set":
                    raise ValueError("Novas operações devem somar ou retirar do estoque.")
                archived = self.db.execute("SELECT * FROM operation_types WHERE name=? COLLATE NOCASE", (clean_name,)).fetchone()
                if archived:
                    if archived["active"] or archived["protected"]:
                        raise ValueError("Já existe uma operação com esse nome.")
                    self.db.execute("UPDATE operation_types SET effect=?,active=1 WHERE id=?", (effect, archived["id"]))
                    return int(archived["id"])
                cursor = self.db.execute("""INSERT INTO operation_types(name,effect,active,protected,created_at)
                    VALUES(?,?,1,0,?)""", (clean_name, effect, datetime.now().isoformat(timespec="seconds")))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("Já existe uma operação com esse nome.") from error

    def delete_operation(self, operation_id: int) -> None:
        operation = self.operation(operation_id)
        if not operation:
            raise ValueError("Essa operação não existe mais.")
        with self.db:
            self.db.execute("UPDATE operation_types SET active=0 WHERE id=?", (operation_id,))

    def _resolve_operation(self, operation: int | str) -> tuple[sqlite3.Row, str]:
        definition = self.operation(operation)
        if not definition:
            raise ValueError("Selecione uma operação válida.")
        kind = definition["legacy_type"]
        if not kind:
            kind = "entrada" if definition["effect"] == "positive" else "saida"
        return definition, kind

    def users(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        where = "" if include_inactive else "WHERE active=1"
        return self.db.execute(f"SELECT * FROM users {where} ORDER BY name COLLATE NOCASE").fetchall()

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def save_user(self, name: str, user_id: int | None = None) -> int:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Informe o nome do usuário.")
        if len(clean_name) > 60:
            raise ValueError("O nome do usuário pode ter no máximo 60 caracteres.")
        try:
            with self.db:
                if user_id:
                    if not self.user(user_id):
                        raise ValueError("Esse usuário não existe mais.")
                    self.db.execute("UPDATE users SET name=?,active=1 WHERE id=?", (clean_name, user_id))
                    return user_id
                archived = self.db.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE", (clean_name,)).fetchone()
                if archived:
                    if archived["active"]:
                        raise ValueError("Já existe um usuário com esse nome.")
                    self.db.execute("UPDATE users SET active=1 WHERE id=?", (archived["id"],))
                    return int(archived["id"])
                cursor = self.db.execute("INSERT INTO users(name,active,created_at) VALUES(?,1,?)", (clean_name, datetime.now().isoformat(timespec="seconds")))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("Já existe um usuário com esse nome.") from error

    def delete_user(self, user_id: int) -> None:
        if not self.user(user_id):
            raise ValueError("Esse usuário não existe mais.")
        with self.db:
            self.db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))

    def products(self, search: str = "") -> list[sqlite3.Row]:
        rows = self._cached_products()
        query = search.strip()
        return [row for row in rows if product_matches_search(row, query)] if query else list(rows)

    def groups(self) -> list[str]:
        return [row["name"] for row in self.db.execute("SELECT name FROM product_groups WHERE active=1 ORDER BY name COLLATE NOCASE")]

    def group_records(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        where = "" if include_inactive else "WHERE active=1"
        return self.db.execute(f"SELECT * FROM product_groups {where} ORDER BY name COLLATE NOCASE").fetchall()

    def group(self, group_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM product_groups WHERE id=?", (group_id,)).fetchone()

    def save_group(self, name: str, group_id: int | None = None) -> int:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Informe o nome do grupo.")
        if len(clean_name) > 60:
            raise ValueError("O nome do grupo pode ter no máximo 60 caracteres.")
        try:
            with self.db:
                if group_id:
                    current = self.group(group_id)
                    if not current:
                        raise ValueError("Esse grupo não existe mais.")
                    self.db.execute("UPDATE product_groups SET name=?,active=1 WHERE id=?", (clean_name, group_id))
                    self.db.execute("UPDATE products SET group_name=? WHERE group_name=? COLLATE NOCASE", (clean_name, current["name"]))
                    return group_id
                archived = self.db.execute("SELECT * FROM product_groups WHERE name=? COLLATE NOCASE", (clean_name,)).fetchone()
                if archived:
                    if archived["active"]:
                        raise ValueError("Já existe um grupo com esse nome.")
                    self.db.execute("UPDATE product_groups SET active=1 WHERE id=?", (archived["id"],))
                    return int(archived["id"])
                cursor = self.db.execute("INSERT INTO product_groups(name,active,created_at) VALUES(?,1,?)", (clean_name, datetime.now().isoformat(timespec="seconds")))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("Já existe um grupo com esse nome.") from error

    def delete_group(self, group_id: int) -> None:
        group = self.group(group_id)
        if not group:
            raise ValueError("Esse grupo não existe mais.")
        if self.db.execute("SELECT 1 FROM products WHERE group_name=? COLLATE NOCASE LIMIT 1", (group["name"],)).fetchone():
            raise ValueError("Esse grupo está sendo usado por produtos e não pode ser apagado.")
        with self.db:
            self.db.execute("UPDATE product_groups SET active=0 WHERE id=?", (group_id,))

    def product(self, product_id: int) -> sqlite3.Row | None:
        self._cached_products()
        return self._products_by_id.get(int(product_id))

    def duplicate_product(self, name: str, group_name: str, product_id: int | None = None) -> sqlite3.Row | None:
        query = """SELECT id,name,group_name FROM products
            WHERE normalize_identity_text(name)=?
              AND normalize_identity_text(group_name)=?"""
        arguments: list[object] = [normalize_identity_text(name), normalize_identity_text(group_name)]
        if product_id is not None:
            query += " AND id<>?"
            arguments.append(int(product_id))
        query += " ORDER BY id LIMIT 1"
        return self.db.execute(query, arguments).fetchone()

    def save_product(self, values: dict, product_id: int | None = None) -> None:
        name = " ".join(str(values.get("name") or "").strip().split())
        group_name = " ".join(str(values.get("group_name") or "").strip().split())
        if not name:
            raise ValueError("Informe o nome do produto.")
        if product_id is not None and not self.product(int(product_id)):
            raise ValueError("Esse produto não existe mais.")
        if self.duplicate_product(name, group_name, product_id):
            group_label = group_name or "Sem grupo"
            raise ValueError(f"Já existe o produto “{name}” no grupo “{group_label}”.")
        fields = (
            name,
            " ".join(str(values.get("category") or "").strip().split()),
            group_name,
            " ".join(str(values.get("variant") or "").strip().split()),
            str(values.get("unit") or "un").strip() or "un",
            values.get("minimum", 0),
            str(values.get("photo") or ""),
            str(values.get("notes") or "").strip(),
        )
        try:
            with self.db:
                if product_id is not None:
                    self.db.execute("UPDATE products SET name=?,category=?,group_name=?,variant=?,unit=?,minimum=?,photo=?,notes=? WHERE id=?", fields + (int(product_id),))
                else:
                    self.db.execute("INSERT INTO products(name,category,group_name,variant,unit,minimum,photo,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?)", fields + (datetime.now().isoformat(timespec="seconds"),))
        except sqlite3.IntegrityError as error:
            if "duplicate_product_same_group" in str(error):
                group_label = group_name or "Sem grupo"
                raise ValueError(f"Já existe o produto “{name}” no grupo “{group_label}”.") from error
            raise

    def delete_product(self, product_id: int) -> bool:
        if self.db.execute("SELECT 1 FROM movements WHERE product_id=? LIMIT 1", (product_id,)).fetchone():
            return False
        self.db.execute("DELETE FROM products WHERE id=?", (product_id,)); self.db.commit(); return True

    def sku_mappings(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.db.execute("""SELECT sm.*,COUNT(smp.product_id) product_count,
            GROUP_CONCAT(
                COALESCE(NULLIF(TRIM(p.group_name),'') || ' • ','') || p.name ||
                COALESCE(' • ' || NULLIF(TRIM(p.variant),''),''),
                '; '
            ) product_labels
            FROM sku_mappings sm
            LEFT JOIN sku_mapping_products smp ON smp.mapping_id=sm.id
            LEFT JOIN products p ON p.id=smp.product_id
            WHERE sm.sku LIKE ? OR EXISTS(
                SELECT 1 FROM sku_mapping_products lookup
                JOIN products p ON p.id=lookup.product_id
                WHERE lookup.mapping_id=sm.id AND (
                    p.name LIKE ? OR p.group_name LIKE ? OR p.variant LIKE ? OR p.category LIKE ?
                )
            )
            GROUP BY sm.id ORDER BY sm.sku COLLATE NOCASE""", (term, term, term, term, term)).fetchall()

    def sku_mapping(self, mapping_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sku_mappings WHERE id=?", (mapping_id,)).fetchone()

    def sku_mapping_for(self, sku: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sku_mappings WHERE normalized_sku=?", (normalize_sku_key(sku),)).fetchone()

    def sku_mapping_products(self, mapping_id: int) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT p.*,smp.quantity_per_sale,COALESCE(SUM(m.quantity),0) stock
            FROM sku_mapping_products smp
            JOIN products p ON p.id=smp.product_id
            LEFT JOIN movements m ON m.product_id=p.id
            WHERE smp.mapping_id=?
            GROUP BY p.id,smp.quantity_per_sale
            ORDER BY p.group_name COLLATE NOCASE,p.name COLLATE NOCASE,p.variant COLLATE NOCASE""", (mapping_id,)).fetchall()

    def save_sku_mapping(self, sku: str, product_ids: list[int], mapping_id: int | None = None) -> int:
        clean_sku = " ".join(sku.strip().split())
        normalized = normalize_sku_key(clean_sku)
        if not normalized:
            raise ValueError("Informe o SKU.")
        if len(clean_sku) > 180:
            raise ValueError("O SKU pode ter no máximo 180 caracteres.")
        unique_products = list(dict.fromkeys(int(product_id) for product_id in product_ids))
        if not unique_products:
            raise ValueError("Selecione pelo menos um produto que será descontado.")
        existing_products = {
            int(row["id"]) for row in self.db.execute(
                f"SELECT id FROM products WHERE id IN ({','.join('?' for _ in unique_products)})",
                unique_products,
            )
        }
        if existing_products != set(unique_products):
            raise ValueError("Um dos produtos selecionados não existe mais.")
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with self.db:
                if mapping_id:
                    if not self.sku_mapping(mapping_id):
                        raise ValueError("Esse vínculo de SKU não existe mais.")
                    self.db.execute("UPDATE sku_mappings SET sku=?,normalized_sku=?,updated_at=? WHERE id=?", (clean_sku, normalized, now, mapping_id))
                else:
                    existing = self.sku_mapping_for(clean_sku)
                    if existing:
                        mapping_id = int(existing["id"])
                        self.db.execute("UPDATE sku_mappings SET sku=?,updated_at=? WHERE id=?", (clean_sku, now, mapping_id))
                    else:
                        cursor = self.db.execute("INSERT INTO sku_mappings(sku,normalized_sku,created_at,updated_at) VALUES(?,?,?,?)", (clean_sku, normalized, now, now))
                        mapping_id = int(cursor.lastrowid)
                self.db.execute("DELETE FROM sku_mapping_products WHERE mapping_id=?", (mapping_id,))
                self.db.executemany(
                    "INSERT INTO sku_mapping_products(mapping_id,product_id,quantity_per_sale) VALUES(?,?,1)",
                    ((mapping_id, product_id) for product_id in unique_products),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("Já existe um vínculo cadastrado para esse SKU.") from error
        return int(mapping_id)

    def delete_sku_mapping(self, mapping_id: int) -> None:
        if not self.sku_mapping(mapping_id):
            raise ValueError("Esse vínculo de SKU não existe mais.")
        with self.db:
            self.db.execute("DELETE FROM sku_mappings WHERE id=?", (mapping_id,))

    def stock(self, product_id: int) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(quantity),0) value FROM movements WHERE product_id=?", (product_id,)).fetchone()["value"])

    def negative_stock_products(self) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT p.id,p.name,p.group_name,p.variant,p.unit,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id
            GROUP BY p.id
            HAVING COALESCE(SUM(m.quantity),0) < -0.0000001
            ORDER BY p.name COLLATE NOCASE,p.group_name COLLATE NOCASE,p.variant COLLATE NOCASE""").fetchall()

    def _balance_before(self, product_id: int, movement_date: str, created_at: str, movement_id: int) -> float:
        return float(self.db.execute("""SELECT COALESCE(SUM(quantity),0) value FROM movements
            WHERE product_id=? AND id<>? AND (
                movement_date<? OR (movement_date=? AND (created_at<? OR (created_at=? AND id<?)))
            )""", (product_id, movement_id, movement_date, movement_date, created_at, created_at, movement_id)).fetchone()["value"])

    @staticmethod
    def _movement_delta(kind: str, informed: float, balance_before: float) -> float:
        if kind in ("entrada", "saida") and informed <= 0:
            raise ValueError("A quantidade deve ser maior que zero.")
        if kind in ("ajuste", "inventario") and informed < 0:
            raise ValueError("A nova contagem não pode ser negativa.")
        delta = -informed if kind == "saida" else informed
        if kind in ("ajuste", "inventario"):
            delta = informed - balance_before
        if abs(delta) < .0000001 and kind != "inventario":
            raise ValueError("A quantidade informada já é o saldo nesse momento.")
        return delta

    def _recalculate_product(self, product_id: int) -> None:
        balance = 0.0
        rows = self.db.execute("""SELECT id,type,quantity,informed_quantity,movement_date FROM movements WHERE product_id=?
            ORDER BY movement_date,created_at,id""", (product_id,)).fetchall()
        for row in rows:
            quantity = float(row["quantity"])
            if row["type"] in ("ajuste", "inventario") and row["informed_quantity"] is not None:
                target = float(row["informed_quantity"])
                quantity = target - balance
                balance = target
            else:
                balance += quantity
            self.db.execute("UPDATE movements SET quantity=?,resulting_stock=? WHERE id=?", (quantity, balance, row["id"]))

    def add_movement_batch(self, operation: int | str, items: list[tuple[int, float]], movement_date: str, reason: str, performed_by: str) -> int:
        definition, kind = self._resolve_operation(operation)
        internal_inventory = str(operation) == "inventario" and definition["legacy_type"] == "inventario"
        if not definition["active"] and not internal_inventory:
            raise ValueError("Essa operação foi apagada. Escolha outra operação.")
        if not items:
            raise ValueError("Adicione pelo menos um produto à movimentação.")
        responsible = " ".join(performed_by.strip().split())
        if not responsible:
            raise ValueError("Informe o usuário responsável pela movimentação.")
        unique_products: set[int] = set()
        for product_id, _informed in items:
            if product_id in unique_products:
                raise ValueError("O mesmo produto não pode aparecer duas vezes no conjunto.")
            unique_products.add(product_id)
        created_at = datetime.now().isoformat(timespec="microseconds")
        with self.db:
            batch = self.db.execute("""INSERT INTO movement_batches(operation_id,movement_date,reason,performed_by,created_at)
                VALUES(?,?,?,?,?)""", (definition["id"], movement_date, reason.strip(), responsible, created_at))
            batch_id = int(batch.lastrowid)
            for index, (product_id, informed) in enumerate(items):
                item_created_at = f"{created_at}-{index:04d}"
                balance_before = self._balance_before(product_id, movement_date, item_created_at, 2**63-1)
                delta = self._movement_delta(kind, informed, balance_before)
                informed_quantity = informed if kind in ("ajuste", "inventario") else None
                self.db.execute("""INSERT INTO movements(product_id,type,quantity,resulting_stock,informed_quantity,movement_date,reason,checked_by,created_at,operation_id,batch_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (product_id, kind, delta, balance_before + delta, informed_quantity, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), responsible, item_created_at, definition["id"], batch_id))
            for product_id in unique_products:
                self._recalculate_product(product_id)
        return batch_id

    def add_movement(self, product_id: int, operation: int | str, informed: float, movement_date: str, reason: str, checked_by: str = "") -> None:
        responsible = checked_by.strip() or "Contagem"
        self.add_movement_batch(operation, [(product_id, informed)], movement_date, reason, responsible)

    def add_quick_stock_movement(
        self,
        action: str,
        product_id: int,
        movement_date: str,
        performed_by: str,
    ) -> int:
        """Register exactly one defective or returned unit using the matching operation."""
        definition = QUICK_STOCK_ACTIONS.get(action)
        if not definition:
            raise ValueError("Escolha Defeito ou Devolução.")
        operation_name, expected_effect, _delta = definition
        operation = self.operation(operation_name)
        effect_label = "negativo" if expected_effect == "negative" else "positivo"
        if not operation or not operation["active"]:
            raise ValueError(
                f"Cadastre ou reative a operação “{operation_name}” com efeito {effect_label} na aba Cadastro."
            )
        if operation["effect"] != expected_effect:
            raise ValueError(
                f"A operação “{operation_name}” precisa ter efeito {effect_label}. Corrija-a na aba Cadastro."
            )
        if not self.product(int(product_id)):
            raise ValueError("Esse produto não existe mais.")
        return self.add_movement_batch(
            int(operation["id"]),
            [(int(product_id), 1)],
            movement_date,
            f"Registro rápido: {action}",
            performed_by,
        )

    def _validate_kit_conversion(
        self,
        mode: str,
        source_product_id: int,
        target_product_id: int,
        quantity: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row, float, sqlite3.Row]:
        if mode not in KIT_INTERNAL_OPERATIONS:
            raise ValueError("Escolha Montagem ou Desmontagem.")
        source = self.product(int(source_product_id))
        target = self.product(int(target_product_id))
        if not source or not target:
            raise ValueError("Um dos kits selecionados não existe mais.")
        if int(source["id"]) == int(target["id"]):
            raise ValueError("O kit de origem e o kit de destino devem ser diferentes.")
        amount = float(quantity)
        if not math.isfinite(amount) or amount <= 0 or not amount.is_integer():
            raise ValueError("A quantidade deve ser um número inteiro maior que zero.")

        source_count = kit_piece_count(source)
        target_count = kit_piece_count(target)
        if source_count not in KIT_PIECE_COUNTS or target_count not in KIT_PIECE_COUNTS:
            raise ValueError("Use somente produtos dos grupos de 2, 4 ou 5 peças.")
        if kit_variation_key(source) != kit_variation_key(target):
            raise ValueError("A origem e o destino precisam ser exatamente da mesma cor/variação.")
        if str(source["unit"]) != str(target["unit"]):
            raise ValueError("Os kits de origem e destino precisam usar a mesma unidade.")
        if mode == "montagem" and not source_count < target_count:
            raise ValueError("Na montagem, use um kit menor como origem e um kit maior como destino.")
        if mode == "desmembramento" and not source_count > target_count:
            raise ValueError("Na desmontagem, use um kit maior como origem e um kit menor como destino.")

        operation = self.operation(KIT_INTERNAL_OPERATIONS[mode])
        if not operation:
            raise ValueError("A operação interna dessa conversão não foi encontrada.")
        return source, target, amount, operation

    @staticmethod
    def _kit_conversion_reason(mode: str, source: sqlite3.Row, target: sqlite3.Row, note: str) -> str:
        title = "Montagem" if mode == "montagem" else "Desmontagem"
        route = f"{title}: {product_label(source)} → {product_label(target)}"
        clean_note = " ".join(note.strip().split())
        return f"{route} — {clean_note}" if clean_note else route

    def _insert_kit_conversion_items(
        self,
        batch_id: int,
        operation_id: int,
        source: sqlite3.Row,
        target: sqlite3.Row,
        amount: float,
        movement_date: str,
        reason: str,
        responsible: str,
        created_at: str,
    ) -> set[int]:
        affected: set[int] = set()
        for index, (product, kind, delta) in enumerate((
            (source, "saida", -amount),
            (target, "entrada", amount),
        )):
            product_id = int(product["id"])
            affected.add(product_id)
            item_created_at = f"{created_at}-{index:04d}"
            balance_before = self._balance_before(product_id, movement_date, item_created_at, 2**63 - 1)
            self.db.execute("""INSERT INTO movements(product_id,type,quantity,resulting_stock,informed_quantity,movement_date,reason,checked_by,created_at,operation_id,batch_id)
                VALUES(?,?,?,?,NULL,?,?,?,?,?,?)""", (
                product_id, kind, delta, balance_before + delta, movement_date,
                reason, responsible, item_created_at, operation_id, batch_id,
            ))
        return affected

    def add_kit_conversion(
        self,
        mode: str,
        source_product_id: int,
        target_product_id: int,
        quantity: float,
        movement_date: str,
        note: str,
        performed_by: str,
    ) -> int:
        source, target, amount, operation = self._validate_kit_conversion(
            mode, source_product_id, target_product_id, quantity
        )
        responsible = " ".join(performed_by.strip().split())
        if not responsible:
            raise ValueError("Informe o usuário responsável pela conversão.")
        try:
            datetime.strptime(movement_date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("Informe uma data válida.") from error
        reason = self._kit_conversion_reason(mode, source, target, note)
        created_at = datetime.now().isoformat(timespec="microseconds")
        with self.db:
            cursor = self.db.execute("""INSERT INTO movement_batches(operation_id,movement_date,reason,performed_by,created_at)
                VALUES(?,?,?,?,?)""", (operation["id"], movement_date, reason, responsible, created_at))
            batch_id = int(cursor.lastrowid)
            affected = self._insert_kit_conversion_items(
                batch_id, int(operation["id"]), source, target, amount,
                movement_date, reason, responsible, created_at,
            )
            for product_id in affected:
                self._recalculate_product(product_id)
        return batch_id

    def kit_conversion_batch(self, batch_id: int) -> dict | None:
        batch = self.movement_batch(batch_id)
        if not batch or batch["legacy_type"] not in KIT_INTERNAL_OPERATIONS.values():
            return None
        items = self.movement_batch_items(batch_id)
        source = next((item for item in items if float(item["quantity"]) < 0), None)
        target = next((item for item in items if float(item["quantity"]) > 0), None)
        if not source or not target:
            return None
        mode = "montagem" if batch["legacy_type"] == KIT_INTERNAL_OPERATIONS["montagem"] else "desmembramento"
        return {
            "batch": batch,
            "mode": mode,
            "source": source,
            "target": target,
            "quantity": abs(float(source["quantity"])),
        }

    def update_kit_conversion(
        self,
        batch_id: int,
        mode: str,
        source_product_id: int,
        target_product_id: int,
        quantity: float,
        movement_date: str,
        note: str,
        performed_by: str,
    ) -> None:
        current = self.kit_conversion_batch(batch_id)
        if not current:
            raise ValueError("Essa montagem ou desmontagem não existe mais.")
        source, target, amount, operation = self._validate_kit_conversion(
            mode, source_product_id, target_product_id, quantity
        )
        responsible = " ".join(performed_by.strip().split())
        if not responsible:
            raise ValueError("Informe o usuário responsável pela conversão.")
        try:
            datetime.strptime(movement_date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("Informe uma data válida.") from error
        reason = self._kit_conversion_reason(mode, source, target, note)
        batch = current["batch"]
        previous_items = self.movement_batch_items(batch_id)
        affected = {int(item["product_id"]) for item in previous_items}
        with self.db:
            self.db.execute("DELETE FROM movements WHERE batch_id=?", (batch_id,))
            self.db.execute("""UPDATE movement_batches SET operation_id=?,movement_date=?,reason=?,performed_by=? WHERE id=?""", (
                operation["id"], movement_date, reason, responsible, batch_id,
            ))
            affected.update(self._insert_kit_conversion_items(
                batch_id, int(operation["id"]), source, target, amount,
                movement_date, reason, responsible, str(batch["created_at"]),
            ))
            for product_id in affected:
                self._recalculate_product(product_id)

    @staticmethod
    def _confidence_from_rows(product: sqlite3.Row, movements: list[sqlite3.Row], current_stock: float, today: date) -> dict:
        """Calculate confidence from already loaded rows so list pages avoid N+1 queries."""
        last_count = next((movement for movement in reversed(movements) if movement["type"] == "inventario"), None)
        if last_count:
            anchor_key = (last_count["movement_date"], last_count["created_at"], int(last_count["id"]))
            activity = [
                movement for movement in movements
                if (movement["movement_date"], movement["created_at"], int(movement["id"])) > anchor_key
            ]
            anchor = datetime.strptime(last_count["movement_date"], "%Y-%m-%d").date();base = 100.0
        else:
            activity = movements
            first = movements[0]["movement_date"] if movements else ""
            anchor = datetime.strptime(first or product["created_at"][:10], "%Y-%m-%d").date();base = 45.0
        days = max(0, (today - anchor).days)
        movement_count = len(activity)
        moved_units = sum(abs(float(row["quantity"])) for row in activity)
        balance = float(current_stock)
        reference = max(abs(balance), abs(float(last_count["resulting_stock"])) if last_count else 0, 10)
        age_penalty = min(45.0, days * 1.5) if last_count else min(15.0, days * .3)
        movement_penalty = min(20.0, movement_count * 2.0)
        daily_penalty = min(15.0, movement_count / max(days, 1) * 5.0)
        volume_penalty = min(20.0, moved_units / reference * 10.0)
        score = int(round(max(5.0, min(100.0, base - age_penalty - movement_penalty - daily_penalty - volume_penalty))))
        level, _color = confidence_tier(score)
        return {
            "score": score,
            "level": level,
            "checkin": "VERIFICADO" if last_count and score >= 55 else "PENDENTE",
            "last_date": last_count["movement_date"] if last_count else "",
            "checked_by": last_count["checked_by"] if last_count else "",
            "last_difference": float(last_count["quantity"]) if last_count else None,
            "days": days,
            "movement_count": movement_count,
            "moved_units": moved_units,
        }

    def stock_confidences(self, products: list[sqlite3.Row] | None = None, as_of: date | None = None) -> dict[int, dict]:
        """Return confidence for many products with a single movement query."""
        selected = self.products() if products is None else products
        product_ids = {int(product["id"]) for product in selected}
        grouped: dict[int, list[sqlite3.Row]] = {product_id: [] for product_id in product_ids}
        if product_ids:
            placeholders = ",".join("?" for _ in product_ids)
            rows = self.db.execute(
                f"SELECT * FROM movements WHERE product_id IN ({placeholders}) ORDER BY product_id,movement_date,created_at,id",
                tuple(sorted(product_ids)),
            ).fetchall()
            for movement in rows:grouped[int(movement["product_id"])].append(movement)
        today = as_of or date.today()
        return {
            int(product["id"]): self._confidence_from_rows(
                product, grouped[int(product["id"])], float(product["stock"]), today,
            )
            for product in selected
        }

    def stock_confidence(self, product_id: int, current_stock: float | None = None, as_of: date | None = None) -> dict:
        """Estimate balance reliability from age and activity since the last physical count."""
        product = self.product(product_id)
        if not product:raise ValueError("Esse produto não existe mais.")
        movements = self.db.execute(
            "SELECT * FROM movements WHERE product_id=? ORDER BY movement_date,created_at,id", (product_id,),
        ).fetchall()
        balance = float(product["stock"]) if current_stock is None else float(current_stock)
        return self._confidence_from_rows(product, movements, balance, as_of or date.today())

    def movement(self, movement_id: int) -> sqlite3.Row | None:
        return self.db.execute("""SELECT m.*,p.name,p.group_name,p.variant,p.unit,
            COALESCE(o.name,CASE m.type WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' WHEN 'ajuste' THEN 'Ajuste' ELSE 'Inventário' END) operation_name
            FROM movements m JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=m.operation_id WHERE m.id=?""", (movement_id,)).fetchone()

    def movement_batch(self, batch_id: int) -> sqlite3.Row | None:
        return self.db.execute("""SELECT mb.*,
            COALESCE(o.name,'Operação removida') operation_name,
            COALESCE(o.effect,'negative') operation_effect,
            COALESCE(o.legacy_type,'') legacy_type
            FROM movement_batches mb
            LEFT JOIN operation_types o ON o.id=mb.operation_id
            WHERE mb.id=?""", (batch_id,)).fetchone()

    def movement_batch_items(self, batch_id: int) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT m.*,p.name,p.group_name,p.variant,p.unit,
            COALESCE(o.name,CASE m.type WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' WHEN 'ajuste' THEN 'Ajuste' ELSE 'Inventário' END) operation_name
            FROM movements m JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=m.operation_id
            WHERE m.batch_id=? ORDER BY m.created_at,m.id""", (batch_id,)).fetchall()

    def movement_history(self, operation: int | str = "todos") -> list[dict]:
        batch_where, legacy_where, args = ("", "", ()) if operation == "todos" else (
            "WHERE mb.operation_id=?", "AND m.operation_id=?", (int(operation),)
        )
        batches = self.db.execute(f"""SELECT mb.id batch_id,NULL movement_id,mb.movement_date,mb.reason,
            mb.performed_by checked_by,mb.created_at,COALESCE(o.name,'Operação removida') operation_name,
            COUNT(m.id) item_count,
            GROUP_CONCAT(CASE WHEN TRIM(COALESCE(p.group_name,''))<>'' THEN p.group_name||' • '||p.name ELSE p.name END,'; ') product_summary
            FROM movement_batches mb
            JOIN movements m ON m.batch_id=mb.id
            JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=mb.operation_id
            {batch_where}
            GROUP BY mb.id""", args).fetchall()
        legacy = self.db.execute(f"""SELECT NULL batch_id,m.id movement_id,m.movement_date,m.reason,m.checked_by,m.created_at,
            COALESCE(o.name,CASE m.type WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' WHEN 'ajuste' THEN 'Ajuste' ELSE 'Inventário' END) operation_name,
            1 item_count,CASE WHEN TRIM(COALESCE(p.group_name,''))<>'' THEN p.group_name||' • '||p.name ELSE p.name END product_summary
            FROM movements m JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=m.operation_id
            WHERE m.batch_id IS NULL {legacy_where}""", args).fetchall()
        history = []
        for row in (*batches, *legacy):
            item = dict(row)
            item["history_key"] = f"batch:{item['batch_id']}" if item["batch_id"] else f"movement:{item['movement_id']}"
            history.append(item)
        history.sort(key=lambda item: (item["movement_date"], item["created_at"]), reverse=True)
        return history[:500]

    def update_movement_batch(self, batch_id: int, operation: int | str, items: list[tuple[int, float]], movement_date: str, reason: str, performed_by: str) -> None:
        batch = self.movement_batch(batch_id)
        if not batch:
            raise ValueError("Essa movimentação não existe mais.")
        previous_items = self.movement_batch_items(batch_id)
        definition, kind = self._resolve_operation(operation)
        internal_inventory = str(operation) == "inventario" and definition["legacy_type"] == "inventario"
        if not definition["active"] and not internal_inventory:
            raise ValueError("Essa operação foi apagada. Escolha outra operação.")
        if not items:
            raise ValueError("Adicione pelo menos um produto à movimentação.")
        responsible = " ".join(performed_by.strip().split())
        if not responsible:
            raise ValueError("Informe o usuário responsável pela movimentação.")
        unique_products: set[int] = set()
        for product_id, informed in items:
            if product_id in unique_products:
                raise ValueError("O mesmo produto não pode aparecer duas vezes no conjunto.")
            if not math.isfinite(float(informed)):
                raise ValueError("Revise a quantidade informada.")
            unique_products.add(product_id)
        affected = unique_products | {int(item["product_id"]) for item in previous_items}
        created_at = str(batch["created_at"])
        with self.db:
            self.db.execute("DELETE FROM movements WHERE batch_id=?", (batch_id,))
            self.db.execute("""UPDATE movement_batches SET operation_id=?,movement_date=?,reason=?,performed_by=? WHERE id=?""",
                (definition["id"], movement_date, reason.strip(), responsible, batch_id))
            for index, (product_id, informed) in enumerate(items):
                item_created_at = f"{created_at}-{index:04d}"
                balance_before = self._balance_before(product_id, movement_date, item_created_at, 2**63-1)
                delta = self._movement_delta(kind, informed, balance_before)
                informed_quantity = informed if kind in ("ajuste", "inventario") else None
                self.db.execute("""INSERT INTO movements(product_id,type,quantity,resulting_stock,informed_quantity,movement_date,reason,checked_by,created_at,operation_id,batch_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (product_id, kind, delta, balance_before + delta, informed_quantity, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), responsible, item_created_at, definition["id"], batch_id))
            for product_id in affected:
                self._recalculate_product(product_id)

    def delete_movement_batch(self, batch_id: int) -> None:
        items = self.movement_batch_items(batch_id)
        if not items:
            raise ValueError("Essa movimentação não existe mais.")
        affected = {int(item["product_id"]) for item in items}
        with self.db:
            self.db.execute("DELETE FROM movements WHERE batch_id=?", (batch_id,))
            self.db.execute("DELETE FROM movement_batches WHERE id=?", (batch_id,))
            for product_id in affected:
                self._recalculate_product(product_id)

    def update_movement(self, movement_id: int, product_id: int, operation: int | str, informed: float, movement_date: str, reason: str, checked_by: str) -> None:
        previous = self.movement(movement_id)
        if not previous:
            raise ValueError("A movimentação não existe mais.")
        definition, kind = self._resolve_operation(operation)
        balance_before = self._balance_before(product_id, movement_date, previous["created_at"], movement_id)
        delta = self._movement_delta(kind, informed, balance_before)
        informed_quantity = informed if kind in ("ajuste", "inventario") else None
        affected = {int(previous["product_id"]), product_id}
        with self.db:
            responsible = " ".join(checked_by.strip().split())
            if not responsible:
                raise ValueError("Informe o usuário responsável pela movimentação.")
            self.db.execute("""UPDATE movements SET product_id=?,type=?,quantity=?,informed_quantity=?,movement_date=?,reason=?,operation_id=?,checked_by=?
                WHERE id=?""", (product_id, kind, delta, informed_quantity, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), definition["id"], responsible, movement_id))
            for affected_product in affected:
                self._recalculate_product(affected_product)

    def delete_movement(self, movement_id: int) -> None:
        movement = self.movement(movement_id)
        if not movement:
            raise ValueError("A movimentação não existe mais.")
        with self.db:
            self.db.execute("DELETE FROM movements WHERE id=?", (movement_id,))
            self._recalculate_product(int(movement["product_id"]))
            if movement["batch_id"]:
                self.db.execute("DELETE FROM movement_batches WHERE id=? AND NOT EXISTS(SELECT 1 FROM movements WHERE batch_id=?)", (movement["batch_id"], movement["batch_id"]))

    def movements(self, operation: int | str = "todos") -> list[sqlite3.Row]:
        where, args = ("", ()) if operation == "todos" else ("WHERE m.operation_id=?", (int(operation),))
        return self.db.execute(f"""SELECT m.*,p.name,p.group_name,p.variant,p.unit,
            COALESCE(o.name,CASE m.type WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' WHEN 'ajuste' THEN 'Ajuste' ELSE 'Inventário' END) operation_name
            FROM movements m JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=m.operation_id
            {where} ORDER BY m.movement_date DESC,m.created_at DESC LIMIT 500""", args).fetchall()

    def backup(self, target: Path) -> None:
        self.db.commit(); shutil.copy2(self.path, target)

    def restore(self, source: Path) -> None:
        try:
            candidate = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise ValueError("O arquivo escolhido não é um banco de dados SQLite válido.") from error
        try:
            core_tables = {
                str(row[0])
                for row in candidate.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"products", "movements"}.issubset(core_tables):
                raise ValueError("O arquivo escolhido não é um backup válido do Estoque Bolsas Baby.")
            errors = database_integrity_errors(candidate)
            if errors:
                raise ValueError("O backup está corrompido: " + "; ".join(errors[:3]))
        except sqlite3.Error as error:
            raise ValueError("O arquivo escolhido não é um banco de dados SQLite válido.") from error
        finally:
            candidate.close()
        safety_backup = self.path.with_name(f"antes-da-restauracao-{datetime.now():%Y%m%d-%H%M%S}.db")
        self.db.commit()
        shutil.copy2(self.path, safety_backup)
        self.db.close()
        try:
            shutil.copy2(source, self.path)
            self.__init__()
        except Exception:
            shutil.copy2(safety_backup, self.path)
            self.__init__()
            raise

class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["surface"], corner_radius=12, border_width=1, border_color=COLORS["border"], **kwargs)


class PageTitle(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str):
        super().__init__(master, fg_color="transparent")
        ctk.CTkLabel(self, text=title, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 25, "bold")).pack(anchor="w")
        ctk.CTkLabel(self, text=subtitle, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 12)).pack(anchor="w", pady=(5, 0))


def apply_window_icon(window: tk.Misc) -> None:
    window._application_icon_photo = ImageTk.PhotoImage(app_icon(256))
    window.iconphoto(True, window._application_icon_photo)
    try:
        window.iconbitmap(str(application_icon_path()))
    except tk.TclError:
        pass


class BrandedToplevel(ctk.CTkToplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_window_icon(self)


class ProductDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp", product: sqlite3.Row | None = None):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent, self.product, self.result = parent, product, None
        self.title("Editar produto" if product else "Novo produto")
        scale = parent.ui_scale
        width, height = round(620 * scale), round(650 * scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+80}+{parent.winfo_y()+50}")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="PRODUTO", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=28, pady=(22, 2))
        ctk.CTkLabel(header, text="Editar produto" if product else "Novo produto", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 23, "bold")).pack(anchor="w", padx=28, pady=(0, 22))
        form = SmoothScrollableFrame(self, fg_color="transparent", corner_radius=0)
        form.grid(row=1, column=0, sticky="nsew", padx=24, pady=20); self.grid_rowconfigure(1, weight=1); form.grid_columnconfigure((0, 1), weight=1)
        self.name = self.field(form, "Nome do produto *", 0, 0, product["name"] if product else "", 2)
        self.category = self.field(form, "Categoria", 2, 0, product["category"] if product else "")
        group_values = ["Sem grupo", *parent.db.groups()]
        selected_group = product["group_name"] if product and product["group_name"] else "Sem grupo"
        if selected_group not in group_values:
            group_values.append(selected_group)
        self.group_name = self.field(form, "Grupo / modelo", 2, 1, selected_group, combo=group_values)
        self.variant = self.field(form, "Variação", 4, 0, product["variant"] if product else "", 2)
        self.unit = self.field(form, "Unidade", 6, 0, product["unit"] if product else "un", combo=["un", "kg", "g", "l", "ml", "cx", "pct"])
        self.minimum = self.field(form, "Estoque mínimo", 6, 1, str(product["minimum"] if product else 0))
        self.photo = product["photo"] if product else ""
        ctk.CTkLabel(form, text="Foto opcional", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=8, column=0, sticky="w", padx=(0, 8), pady=(8, 6))
        self.photo_button = ctk.CTkButton(form, text=Path(self.photo).name if self.photo else "Escolher foto", image=icon("upload", 18), fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.choose_photo)
        self.photo_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(0, 16), ipady=3)
        ctk.CTkLabel(form, text="Observações", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=10, column=0, sticky="w", pady=(0, 6))
        self.notes = ctk.CTkTextbox(form, height=90, corner_radius=9, border_width=1, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.notes.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(0, 20)); self.notes.insert("1.0", product["notes"] if product else "")
        actions = ctk.CTkFrame(form, fg_color="transparent"); actions.grid(row=12, column=0, columnspan=2, sticky="e")
        ctk.CTkButton(actions, text="Cancelar", width=110, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(actions, text="Salvar produto", width=145, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.save).pack(side="left", padx=6)
        self.name.focus_set()

    def field(self, parent, label, row, column, value, span=1, combo=None):
        padx = (0, 8) if column == 0 else (8, 0)
        ctk.CTkLabel(parent, text=label, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=row, column=column, sticky="w", padx=padx, pady=(0, 6))
        widget = ctk.CTkOptionMenu(parent, values=combo, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"]) if combo else ctk.CTkEntry(parent, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        widget.grid(row=row+1, column=column, columnspan=span, sticky="ew", padx=padx, pady=(0, 16), ipady=3); widget.set(value) if combo else widget.insert(0, value); return widget

    def choose_photo(self):
        selected = filedialog.askopenfilename(parent=self, title="Escolher foto", filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp")])
        if selected: self.photo = selected; self.photo_button.configure(text=Path(selected).name)

    def save(self):
        name = self.name.get().strip()
        if not name: messagebox.showwarning(APP_NAME, "Informe o nome do produto.", parent=self); return
        try:
            minimum = float(self.minimum.get().replace(",", ".") or 0)
            if minimum < 0: raise ValueError
        except ValueError: messagebox.showwarning(APP_NAME, "O estoque mínimo deve ser um número positivo.", parent=self); return
        group_name = "" if self.group_name.get() == "Sem grupo" else self.group_name.get().strip()
        product_id = int(self.product["id"]) if self.product else None
        if self.parent.db.duplicate_product(name, group_name, product_id):
            group_label = group_name or "Sem grupo"
            messagebox.showwarning(APP_NAME, f"Já existe o produto “{name}” no grupo “{group_label}”.\n\nEdite o cadastro existente em vez de criar outro.", parent=self)
            return
        photo = self.photo
        if photo and (not self.product or photo != self.product["photo"]):
            source = Path(photo)
            if source.exists():
                target = data_dir()/"fotos"/f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix.lower()}"; shutil.copy2(source, target); photo = str(target)
        self.result = {"name": name, "category": self.category.get().strip(), "group_name": group_name, "variant": self.variant.get().strip(), "unit": self.unit.get(), "minimum": minimum, "photo": photo, "notes": self.notes.get("1.0", "end").strip()}; self.destroy()


class GroupManagerDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp"):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent=parent;self.selected_id: int|None=None;self.title("Gerenciar grupos")
        scale=parent.ui_scale;width,height=round(650*scale),round(470*scale);self.geometry(f"{width}x{height}+{parent.winfo_x()+110}+{parent.winfo_y()+90}");self.minsize(round(580*scale),round(420*scale));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text="GRUPOS DE PRODUTOS",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(20,2));ctk.CTkLabel(header,text="Cadastro de grupos",text_color=COLORS["text"],font=ctk.CTkFont("Inter",23,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text="Organize modelos como 2 PEÇAS, 4 PEÇAS ou qualquer outra família de produtos.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,20))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=22);content.grid_columnconfigure(0,weight=3);content.grid_columnconfigure(1,weight=2);content.grid_rowconfigure(0,weight=1)
        listing=Card(content);listing.grid(row=0,column=0,sticky="nsew",padx=(0,10));self.tree=parent.table(listing,("name",),("Grupo / modelo",),(260,));self.tree.pack(fill="both",expand=True,padx=18,pady=18);self.tree.bind("<<TreeviewSelect>>",self.select_group)
        editor=Card(content);editor.grid(row=0,column=1,sticky="nsew",padx=(10,0));ctk.CTkLabel(editor,text="Nome do grupo",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(anchor="w",padx=18,pady=(20,8));self.name=tk.StringVar();self.name_entry=ctk.CTkEntry(editor,textvariable=self.name,height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.name_entry.pack(fill="x",padx=18,pady=(0,18))
        actions=ctk.CTkFrame(editor,fg_color="transparent");actions.pack(fill="x",padx=18);ctk.CTkButton(actions,text="Novo",width=78,height=38,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.new_group).pack(side="left");ctk.CTkButton(actions,text="Salvar",height=38,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.save_group).pack(side="right",fill="x",expand=True,padx=(8,0));ctk.CTkButton(editor,text="Apagar grupo selecionado",height=36,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_group).pack(fill="x",padx=18,pady=(12,18));self.refresh();self.name_entry.focus_set()
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for group in self.parent.db.group_records():self.tree.insert("","end",iid=str(group["id"]),values=(group["name"],))
    def new_group(self):self.selected_id=None;self.tree.selection_remove(self.tree.selection());self.name.set("");self.name_entry.focus_set()
    def select_group(self,_event=None):
        selected=self.tree.selection()
        if selected:
            group=self.parent.db.group(int(selected[0]));self.selected_id=int(selected[0]);self.name.set(group["name"] if group else "")
    def save_group(self):
        try:saved_id=self.parent.db.save_group(self.name.get(),self.selected_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh();self.selected_id=saved_id;self.tree.selection_set(str(saved_id));self.tree.see(str(saved_id));self.parent.refresh_all()
    def delete_group(self):
        if not self.selected_id:messagebox.showinfo(APP_NAME,"Selecione um grupo para apagar.",parent=self);return
        group=self.parent.db.group(self.selected_id)
        if not group:self.new_group();self.refresh();return
        if not messagebox.askyesno(APP_NAME,f"Apagar o grupo “{group['name']}”?",icon="warning",parent=self):return
        try:self.parent.db.delete_group(self.selected_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.new_group();self.refresh();self.parent.refresh_all()


class UserManagerDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp"):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent = parent; self.selected_id: int | None = None
        self.title("Gerenciar usuários")
        scale = parent.ui_scale; width, height = round(650*scale), round(470*scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+110}+{parent.winfo_y()+90}")
        self.minsize(round(580*scale), round(420*scale)); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text="USUÁRIOS",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(20,2))
        ctk.CTkLabel(header,text="Cadastro de usuários",text_color=COLORS["text"],font=ctk.CTkFont("Inter",23,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text="Esses nomes ficam disponíveis para identificar quem realizou uma movimentação.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,20))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=22);content.grid_columnconfigure(0,weight=3);content.grid_columnconfigure(1,weight=2);content.grid_rowconfigure(0,weight=1)
        listing=Card(content);listing.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        self.tree=parent.table(listing,("name",),("Usuário",),(260,));self.tree.pack(fill="both",expand=True,padx=18,pady=18);self.tree.bind("<<TreeviewSelect>>",self.select_user)
        editor=Card(content);editor.grid(row=0,column=1,sticky="nsew",padx=(10,0))
        ctk.CTkLabel(editor,text="Nome do usuário",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(anchor="w",padx=18,pady=(20,8))
        self.name=tk.StringVar();self.name_entry=ctk.CTkEntry(editor,textvariable=self.name,height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.name_entry.pack(fill="x",padx=18,pady=(0,18))
        actions=ctk.CTkFrame(editor,fg_color="transparent");actions.pack(fill="x",padx=18)
        ctk.CTkButton(actions,text="Novo",width=78,height=38,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.new_user).pack(side="left")
        ctk.CTkButton(actions,text="Salvar",height=38,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.save_user).pack(side="right",fill="x",expand=True,padx=(8,0))
        ctk.CTkButton(editor,text="Apagar usuário selecionado",height=36,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_user).pack(fill="x",padx=18,pady=(12,18))
        self.refresh();self.name_entry.focus_set()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for user in self.parent.db.users():self.tree.insert("","end",iid=str(user["id"]),values=(user["name"],))
    def new_user(self):
        self.selected_id=None;self.tree.selection_remove(self.tree.selection());self.name.set("");self.name_entry.focus_set()
    def select_user(self,_event=None):
        selected=self.tree.selection()
        if selected:
            user=self.parent.db.user(int(selected[0]));self.selected_id=int(selected[0]);self.name.set(user["name"] if user else "")
    def save_user(self):
        try:saved_id=self.parent.db.save_user(self.name.get(),self.selected_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh();self.selected_id=saved_id;self.tree.selection_set(str(saved_id));self.tree.see(str(saved_id));self.parent.refresh_user_controls()
    def delete_user(self):
        if not self.selected_id:messagebox.showinfo(APP_NAME,"Selecione um usuário para apagar.",parent=self);return
        user=self.parent.db.user(self.selected_id)
        if not user:self.new_user();self.refresh();return
        if not messagebox.askyesno(APP_NAME,f"Apagar o usuário “{user['name']}”?\n\nO nome continuará aparecendo nas movimentações antigas.",icon="warning",parent=self):return
        try:self.parent.db.delete_user(self.selected_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.new_user();self.refresh();self.parent.refresh_user_controls()


class ProductManagerDialog(BrandedToplevel):
    def __init__(self,parent:"EstoqueApp"):
        super().__init__(parent,fg_color=COLORS["background"]);self.parent=parent;self.title("Gerenciar produtos")
        scale=parent.ui_scale;width,height=round(920*scale),round(560*scale);self.geometry(f"{width}x{height}+{parent.winfo_x()+60}+{parent.winfo_y()+65}");self.minsize(round(780*scale),round(480*scale));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text="PRODUTOS",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(20,2));ctk.CTkLabel(header,text="Cadastro de produtos",text_color=COLORS["text"],font=ctk.CTkFont("Inter",23,"bold")).pack(anchor="w",padx=28,pady=(0,20))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=22);content.grid_columnconfigure(0,weight=1);content.grid_rowconfigure(1,weight=1)
        toolbar=ctk.CTkFrame(content,fg_color="transparent");toolbar.grid(row=0,column=0,sticky="ew",pady=(0,12));self.search=ctk.CTkEntry(toolbar,placeholder_text="Buscar produto, grupo ou variação...",width=330,height=40,corner_radius=9);self.search.pack(side="left");self.search.bind("<KeyRelease>",lambda _event:self.refresh())
        ctk.CTkButton(toolbar,text="Novo produto",image=parent.icons["plus"],height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.new_product).pack(side="right")
        ctk.CTkButton(toolbar,text="Excluir",image=parent.icons["trash"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_product).pack(side="right",padx=8)
        ctk.CTkButton(toolbar,text="Editar",image=parent.icons["edit"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_product).pack(side="right")
        card=Card(content);card.grid(row=1,column=0,sticky="nsew");self.tree=parent.table(card,("name","group","variant","category","stock"),("Produto","Grupo / modelo","Variação","Categoria","Saldo"),(220,190,170,150,90));self.tree.pack(fill="both",expand=True,padx=18,pady=18);self.tree.bind("<Double-1>",lambda _event:self.edit_product());self.refresh()
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for product in self.parent.db.products(self.search.get()):self.tree.insert("","end",iid=str(product["id"]),values=(product["name"],product["group_name"]or"—",product["variant"]or"—",product["category"]or"—",fmt_number(product["stock"])))
    def selected_product(self):
        selected=self.tree.selection();return int(selected[0]) if selected else None
    def _open_editor(self,product=None):
        self.grab_release();dialog=ProductDialog(self.parent,product);self.wait_window(dialog);self.grab_set()
        if not dialog.result:return
        try:self.parent.db.save_product(dialog.result,int(product["id"]) if product else None)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh();self.parent.refresh_all()
    def new_product(self):self._open_editor()
    def edit_product(self):
        product_id=self.selected_product()
        if not product_id:messagebox.showinfo(APP_NAME,"Selecione um produto para editar.",parent=self);return
        self._open_editor(self.parent.db.product(product_id))
    def delete_product(self):
        product_id=self.selected_product()
        if not product_id:messagebox.showinfo(APP_NAME,"Selecione um produto para excluir.",parent=self);return
        product=self.parent.db.product(product_id)
        if not messagebox.askyesno(APP_NAME,f"Excluir o produto “{product['name']}”?",icon="warning",parent=self):return
        if not self.parent.db.delete_product(product_id):messagebox.showwarning(APP_NAME,"Não é possível excluir um produto com movimentações.",parent=self);return
        self.refresh();self.parent.refresh_all()


class SkuMappingEditorDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp", sku: str = "", mapping_id: int | None = None, locked_sku: bool = False, context: str = ""):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent = parent; self.mapping_id = mapping_id; self.result: int | None = None
        self.title("Vincular SKU a produtos")
        width, height = 820, 560
        geometry = centered_dialog_geometry(parent, width, height)
        fitted = parse_window_geometry(geometry)
        self.geometry(geometry)
        self.minsize(min(680, fitted[0]), min(440, fitted[1])); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())

        mapping = parent.db.sku_mapping(mapping_id) if mapping_id else None
        current_products = parent.db.sku_mapping_products(mapping_id) if mapping_id else []
        self.selected_product_ids = {int(product["id"]) for product in current_products}
        self.products_cache = parent.db.products()
        self.products_by_id = {int(product["id"]): product for product in self.products_cache}
        self._search_job = None

        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0); header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="MEMÓRIA DE SKU", text_color=COLORS["accent"], font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(13,2))
        ctk.CTkLabel(header, text="Escolha os produtos descontados", text_color=COLORS["text"], font=ctk.CTkFont("Inter",21,"bold")).pack(anchor="w",padx=28)
        subtitle = context or "Cada unidade vendida deste SKU descontará uma unidade de cada produto marcado."
        ctk.CTkLabel(header, text=subtitle, wraplength=max(440, fitted[0]-70), justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(4,13))

        content = ctk.CTkFrame(self, fg_color="transparent"); content.grid(row=1,column=0,sticky="nsew",padx=24,pady=14); content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(3,weight=1)
        sku_card = Card(content); sku_card.grid(row=0,column=0,sticky="ew",pady=(0,12)); sku_card.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(sku_card,text="SKU encontrado na lista" if locked_sku else "SKU de venda",anchor="w",text_color=COLORS["text"],font=ctk.CTkFont("Inter",11,"bold")).grid(row=0,column=0,sticky="ew",padx=16,pady=(12,5))
        self.sku = tk.StringVar(value=str(mapping["sku"] if mapping else sku))
        self.sku_entry = ctk.CTkEntry(sku_card,textvariable=self.sku,height=42,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface_alt"])
        self.sku_entry.grid(row=1,column=0,sticky="ew",padx=16,pady=(0,6))
        ctk.CTkLabel(sku_card,text="Marque abaixo todos os produtos que compõem uma unidade deste SKU.",anchor="w",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).grid(row=2,column=0,sticky="ew",padx=16,pady=(0,12))
        if locked_sku:self.sku_entry.configure(state="disabled")
        ctk.CTkLabel(content,text="Produtos vinculados",anchor="w",text_color=COLORS["text"],font=ctk.CTkFont("Inter",12,"bold")).grid(row=1,column=0,sticky="ew",pady=(0,6))
        self.search = ctk.CTkEntry(content,placeholder_text="Buscar produto, grupo, variação ou categoria...",height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"])
        self.search.grid(row=2,column=0,sticky="ew",pady=(0,10)); self.search.bind("<KeyRelease>",self.schedule_product_refresh)
        product_card=Card(content);product_card.grid(row=3,column=0,sticky="nsew");product_card.grid_columnconfigure(0,weight=1);product_card.grid_rowconfigure(1,weight=1)
        self.selection_status=ctk.CTkLabel(product_card,text="",anchor="w",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold"));self.selection_status.grid(row=0,column=0,columnspan=2,sticky="ew",padx=14,pady=(10,6))
        self.product_tree=parent.table(product_card,("selected","product","stock"),("Selecionado","Produto","Saldo"),(90,510,120));self.product_tree.configure(selectmode="none",height=3);self.product_tree.column("product",anchor="w");self.product_tree.grid(row=1,column=0,sticky="nsew",padx=(10,0),pady=(0,10));self.product_tree.bind("<Button-1>",self.toggle_product,add="+")
        product_scrollbar=ctk.CTkScrollbar(product_card,orientation="vertical",command=self.product_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);product_scrollbar.grid(row=1,column=1,sticky="ns",padx=(6,10),pady=(0,10));self.product_tree.configure(yscrollcommand=product_scrollbar.set)
        actions=ctk.CTkFrame(content,fg_color="transparent");actions.grid(row=4,column=0,sticky="ew",pady=(14,0))
        ctk.CTkButton(actions,text="Cancelar",width=105,height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.destroy).pack(side="left")
        ctk.CTkButton(actions,text="Salvar vínculo",width=160,height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.save).pack(side="right")
        self.refresh_products(); self.after(40, lambda: center_native_window(self, parent)); self.after_idle(self.search.focus_set if locked_sku else self.sku_entry.focus_set)

    def refresh_products(self):
        self._search_job=None;self.product_tree.delete(*self.product_tree.get_children());query=self.search.get().strip();visible=0
        dark=ctk.get_appearance_mode()=="Dark";self.product_tree.tag_configure("sku_selected",background="#203C52" if dark else "#DDEFFC")
        for product in self.products_cache:
            if query and not product_matches_search(product,query):continue
            product_id=int(product["id"]);selected=product_id in self.selected_product_ids;visible+=1
            self.product_tree.insert("","end",iid=str(product_id),values=("✓" if selected else "",product_label(product),f"{fmt_number(product['stock'])} {product['unit']}"),tags=("sku_selected",) if selected else ())
        self.update_selection_status(visible)

    def schedule_product_refresh(self,_event=None):
        if self._search_job is not None:self.after_cancel(self._search_job)
        self._search_job=self.after(120,self.refresh_products)

    def toggle_product(self,event):
        if self.product_tree.identify_region(event.x,event.y) not in ("cell","tree"):return
        item_id=self.product_tree.identify_row(event.y)
        if not item_id:return "break"
        product_id=int(item_id)
        if product_id in self.selected_product_ids:self.selected_product_ids.remove(product_id)
        else:self.selected_product_ids.add(product_id)
        product=self.products_by_id[product_id];selected=product_id in self.selected_product_ids
        self.product_tree.item(item_id,values=("✓" if selected else "",product_label(product),f"{fmt_number(product['stock'])} {product['unit']}"),tags=("sku_selected",) if selected else ())
        self.update_selection_status(len(self.product_tree.get_children()))
        return "break"

    def update_selection_status(self,visible: int):
        selected=len(self.selected_product_ids);self.selection_status.configure(text=f"{selected} {'produto selecionado' if selected==1 else 'produtos selecionados'} • {visible} visíveis — clique em uma linha para marcar ou desmarcar")

    def save(self):
        try:self.result=self.parent.db.save_sku_mapping(self.sku.get(),sorted(self.selected_product_ids),self.mapping_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.destroy()


class SkuManagerDialog(BrandedToplevel):
    def __init__(self,parent:"EstoqueApp"):
        super().__init__(parent,fg_color=COLORS["background"]);self.parent=parent;self.title("Gerenciar vínculos de SKU")
        width,height=940,590;geometry=centered_dialog_geometry(parent,width,height);fitted=parse_window_geometry(geometry);self.geometry(geometry);self.minsize(min(760,fitted[0]),min(480,fitted[1]));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text="SKUS DE VENDA",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(18,2));ctk.CTkLabel(header,text="Memória de produtos por SKU",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text="Consulte e altere quais produtos serão descontados nas próximas listas importadas.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,18))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=20);content.grid_columnconfigure(0,weight=1);content.grid_rowconfigure(1,weight=1)
        toolbar=ctk.CTkFrame(content,fg_color="transparent");toolbar.grid(row=0,column=0,sticky="ew",pady=(0,12))
        self._search_job=None
        self.search=ctk.CTkEntry(toolbar,placeholder_text="Buscar SKU ou produto vinculado...",width=360,height=40,corner_radius=9);self.search.pack(side="left");self.search.bind("<KeyRelease>",self.schedule_refresh)
        ctk.CTkButton(toolbar,text="Novo SKU",image=parent.icons["plus"],height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.new_mapping).pack(side="right")
        ctk.CTkButton(toolbar,text="Excluir",image=parent.icons["trash"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_mapping).pack(side="right",padx=8)
        ctk.CTkButton(toolbar,text="Editar vínculo",image=parent.icons["edit"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_mapping).pack(side="right")
        card=Card(content);card.grid(row=1,column=0,sticky="nsew");self.tree=parent.table(card,("sku","products"),("SKU","Produtos descontados"),(260,570));self.tree.column("products",anchor="w");self.tree.pack(fill="both",expand=True,padx=18,pady=18);self.tree.bind("<Double-1>",lambda _event:self.edit_mapping());self.refresh();self.after(40,lambda:center_native_window(self,parent))

    def refresh(self):
        self._search_job=None
        self.tree.delete(*self.tree.get_children())
        for mapping in self.parent.db.sku_mappings(self.search.get()):
            labels=mapping["product_labels"] or "Sem produtos"
            self.tree.insert("","end",iid=str(mapping["id"]),values=(mapping["sku"],labels))

    def schedule_refresh(self,_event=None):
        if self._search_job is not None:self.after_cancel(self._search_job)
        self._search_job=self.after(120,self.refresh)

    def selected_mapping(self):
        selected=self.tree.selection();return int(selected[0]) if selected else None

    def open_editor(self,mapping_id=None):
        self.grab_release();dialog=SkuMappingEditorDialog(self.parent,mapping_id=mapping_id);self.wait_window(dialog);self.grab_set()
        if dialog.result:self.refresh()

    def new_mapping(self):self.open_editor()

    def edit_mapping(self):
        mapping_id=self.selected_mapping()
        if not mapping_id:messagebox.showinfo(APP_NAME,"Selecione um SKU para editar.",parent=self);return
        self.open_editor(mapping_id)

    def delete_mapping(self):
        mapping_id=self.selected_mapping()
        if not mapping_id:messagebox.showinfo(APP_NAME,"Selecione um SKU para excluir.",parent=self);return
        mapping=self.parent.db.sku_mapping(mapping_id)
        if not mapping:self.refresh();return
        if not messagebox.askyesno(APP_NAME,f"Excluir o vínculo do SKU “{mapping['sku']}”?\n\nAs movimentações antigas não serão alteradas.",icon="warning",parent=self):return
        try:self.parent.db.delete_sku_mapping(mapping_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh()


class SalesListReviewDialog(BrandedToplevel):
    def __init__(self,parent:"EstoqueApp",source_label:str,file_name:str,items):
        super().__init__(parent,fg_color=COLORS["background"]);self.parent=parent;self.items=items;self.result=False;self.title(f"Conferir lista {source_label}")
        width,height=1020,600;geometry=centered_dialog_geometry(parent,width,height);fitted=parse_window_geometry(geometry);self.geometry(geometry);self.minsize(min(820,fitted[0]),min(500,fitted[1]));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1);self.bind("<Escape>",lambda _event:self.destroy())
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text=f"LISTA {source_label.upper()}",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(13,2));ctk.CTkLabel(header,text="Confira antes de levar para Movimentações",text_color=COLORS["text"],font=ctk.CTkFont("Inter",21,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text=f"Arquivo: {file_name}",wraplength=max(500,fitted[0]-70),justify="left",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(4,13))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=12);content.grid_columnconfigure(0,weight=1);content.grid_rowconfigure((2,4),weight=1)
        summary_card=Card(content);summary_card.grid(row=0,column=0,sticky="ew",pady=(0,12));self.summary_label=ctk.CTkLabel(summary_card,text="",anchor="w",text_color=COLORS["text"],font=ctk.CTkFont("Inter",11,"bold"));self.summary_label.pack(fill="x",padx=16,pady=11)
        sku_bar=ctk.CTkFrame(content,fg_color="transparent");sku_bar.grid(row=1,column=0,sticky="ew",pady=(0,8));ctk.CTkLabel(sku_bar,text="1. SKUs identificados",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(side="left")
        ctk.CTkButton(sku_bar,text="Alterar vínculo selecionado",image=parent.icons["edit"],height=34,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_mapping).pack(side="right")
        sku_table=ctk.CTkFrame(content,fg_color="transparent");sku_table.grid(row=2,column=0,sticky="nsew");sku_table.grid_columnconfigure(0,weight=1);sku_table.grid_rowconfigure(0,weight=1)
        self.sku_tree=parent.table(sku_table,("sku","quantity","products"),("SKU","Qnt. da lista","Produtos descontados"),(270,110,560));self.sku_tree.configure(height=2);self.sku_tree.column("products",anchor="w");self.sku_tree.grid(row=0,column=0,sticky="nsew");self.sku_tree.bind("<Double-1>",lambda _event:self.edit_mapping())
        sku_scrollbar=ctk.CTkScrollbar(sku_table,orientation="vertical",command=self.sku_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);sku_scrollbar.grid(row=0,column=1,sticky="ns",padx=(7,0));self.sku_tree.configure(yscrollcommand=sku_scrollbar.set)
        ctk.CTkLabel(content,text="2. Baixa consolidada por produto",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).grid(row=3,column=0,sticky="w",pady=(14,8))
        product_table=ctk.CTkFrame(content,fg_color="transparent");product_table.grid(row=4,column=0,sticky="nsew");product_table.grid_columnconfigure(0,weight=1);product_table.grid_rowconfigure(0,weight=1)
        self.product_tree=parent.table(product_table,("product","quantity","current","after"),("Produto","Quantidade a descontar","Saldo atual","Saldo depois"),(500,160,130,130));self.product_tree.configure(height=2);self.product_tree.column("product",anchor="w");self.product_tree.grid(row=0,column=0,sticky="nsew")
        product_scrollbar=ctk.CTkScrollbar(product_table,orientation="vertical",command=self.product_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);product_scrollbar.grid(row=0,column=1,sticky="ns",padx=(7,0));self.product_tree.configure(yscrollcommand=product_scrollbar.set)
        actions=ctk.CTkFrame(content,fg_color="transparent");actions.grid(row=5,column=0,sticky="ew",pady=(14,0));ctk.CTkButton(actions,text="Cancelar",width=105,height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.destroy).pack(side="left")
        ctk.CTkButton(actions,text="Adicionar à movimentação",width=220,height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.confirm).pack(side="right")
        self.refresh()

    def refresh(self):
        self.review_rows,self.product_rows=mapped_sales_list(self.parent.db,self.items);self.sku_tree.delete(*self.sku_tree.get_children());self.product_tree.delete(*self.product_tree.get_children())
        for index,row in enumerate(self.review_rows):self.sku_tree.insert("","end",iid=str(index),values=(row["sku"],fmt_number(row["quantity"]),"; ".join(row["products"])))
        for row in self.product_rows:
            product=row["product"];current=float(product["stock"]);after=current-float(row["quantity"])
            self.product_tree.insert("","end",iid=str(row["product_id"]),values=(product_label(product),f"{fmt_number(row['quantity'])} {product['unit']}",f"{fmt_number(current)} {product['unit']}",f"{fmt_number(after)} {product['unit']}"))
        total_units=sum(float(row["quantity"]) for row in self.product_rows)
        self.summary_label.configure(text=f"{len(self.review_rows)} SKUs lidos   •   {len(self.product_rows)} produtos   •   {fmt_number(total_units)} unidades para descontar")

    def edit_mapping(self):
        selected=self.sku_tree.selection()
        if not selected:messagebox.showinfo(APP_NAME,"Selecione um SKU para alterar o vínculo.",parent=self);return
        row=self.review_rows[int(selected[0])];self.grab_release();dialog=SkuMappingEditorDialog(self.parent,mapping_id=row["mapping_id"]);self.wait_window(dialog);self.grab_set()
        if dialog.result:self.refresh()

    def confirm(self):self.result=True;self.destroy()


class OperationManagerDialog(BrandedToplevel):
    EFFECT_LABELS = {
        "positive": "Soma ao estoque (+)",
        "negative": "Retira do estoque (−)",
        "set": "Define o saldo contado",
    }

    def __init__(self, parent: "EstoqueApp"):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent = parent
        self.selected_id: int | None = None
        self.title("Gerenciar operações")
        scale = parent.ui_scale
        width, height = round(760 * scale), round(500 * scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+90}+{parent.winfo_y()+80}")
        self.minsize(round(680 * scale), round(440 * scale))
        self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="OPERAÇÕES", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=28, pady=(20, 2))
        ctk.CTkLabel(header, text="Gerenciar operações", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 23, "bold")).pack(anchor="w", padx=28)
        ctk.CTkLabel(header, text="Edite ou remova operações padrão e crie entradas ou saídas específicas.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 11)).pack(anchor="w", padx=28, pady=(5, 20))

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=24, pady=22)
        content.grid_columnconfigure(0, weight=3); content.grid_columnconfigure(1, weight=2); content.grid_rowconfigure(0, weight=1)

        listing = Card(content)
        listing.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ctk.CTkLabel(listing, text="Operações disponíveis", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 14, "bold")).pack(anchor="w", padx=18, pady=(18, 10))
        self.tree = parent.table(listing, ("name", "effect"), ("Nome", "Efeito"), (180, 155))
        self.tree.column("name", minwidth=120); self.tree.column("effect", minwidth=120)
        self.tree.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.tree.bind("<<TreeviewSelect>>", self.select_operation)

        editor = Card(content)
        editor.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ctk.CTkLabel(editor, text="Cadastro", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 14, "bold")).pack(anchor="w", padx=18, pady=(18, 14))
        ctk.CTkLabel(editor, text="Nome da operação", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=18, pady=(0, 6))
        self.name = tk.StringVar()
        self.name_entry = ctk.CTkEntry(editor, textvariable=self.name, height=40, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.name_entry.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkLabel(editor, text="O que ela faz?", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=18, pady=(0, 6))
        self.effect = tk.StringVar(value=self.EFFECT_LABELS["positive"])
        self.effect_menu=ctk.CTkOptionMenu(editor, variable=self.effect, values=[self.EFFECT_LABELS["positive"],self.EFFECT_LABELS["negative"]], height=40, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"]);self.effect_menu.pack(fill="x", padx=18, pady=(0, 12))
        self.effect_help=ctk.CTkLabel(editor, text="Exemplo: “Devolução” pode somar e “Avaria” pode retirar.", wraplength=round(230 * scale), justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10));self.effect_help.pack(anchor="w", padx=18, pady=(0, 18))
        actions = ctk.CTkFrame(editor, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkButton(actions, text="Nova", width=82, height=38, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.new_operation).pack(side="left")
        ctk.CTkButton(actions, text="Salvar", height=38, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.save_operation).pack(side="right", fill="x", expand=True, padx=(8, 0))
        self.delete_button = ctk.CTkButton(editor, text="Apagar operação selecionada", height=36, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["danger"], command=self.delete_operation)
        self.delete_button.pack(fill="x", padx=18, pady=(0, 18))
        self.refresh(); self.name_entry.focus_set()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for operation in self.parent.db.operations():
            self.tree.insert("", "end", iid=str(operation["id"]), values=(operation["name"], self.EFFECT_LABELS[operation["effect"]]))

    def new_operation(self):
        self.selected_id = None; self.tree.selection_remove(self.tree.selection())
        self.name.set(""); self.effect.set(self.EFFECT_LABELS["positive"]);self.effect_menu.configure(state="normal",values=[self.EFFECT_LABELS["positive"],self.EFFECT_LABELS["negative"]]);self.effect_help.configure(text="Exemplo: “Devolução” pode somar e “Avaria” pode retirar."); self.name_entry.focus_set()

    def select_operation(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        operation = self.parent.db.operation(int(selected[0]))
        if not operation:
            return
        self.selected_id = int(operation["id"]); self.name.set(operation["name"]); self.effect.set(self.EFFECT_LABELS[operation["effect"]])
        if operation["protected"]:
            self.effect_menu.configure(values=[self.EFFECT_LABELS[operation["effect"]]],state="disabled");self.effect_help.configure(text="O efeito desta operação padrão é fixo para proteger o cálculo do estoque.")
        else:
            self.effect_menu.configure(values=[self.EFFECT_LABELS["positive"],self.EFFECT_LABELS["negative"]],state="normal");self.effect_help.configure(text="Escolha se esta operação soma ou retira unidades do estoque.")

    def save_operation(self):
        effect = next((key for key, label in self.EFFECT_LABELS.items() if label == self.effect.get()), "")
        try:
            saved_id = self.parent.db.save_operation(self.name.get(), effect, self.selected_id)
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error), parent=self); return
        self.refresh(); self.selected_id = saved_id; self.tree.selection_set(str(saved_id)); self.tree.see(str(saved_id)); self.parent.refresh_operation_controls()

    def delete_operation(self):
        if not self.selected_id:
            messagebox.showinfo(APP_NAME, "Selecione uma operação para apagar.", parent=self); return
        operation = self.parent.db.operation(self.selected_id)
        if not operation:
            self.new_operation(); self.refresh(); return
        if not messagebox.askyesno(APP_NAME, f"Apagar a operação “{operation['name']}”?\n\nEla sairá das opções de novas movimentações, mas continuará identificada no histórico.", icon="warning", parent=self):
            return
        try:
            self.parent.db.delete_operation(self.selected_id)
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error), parent=self); return
        self.new_operation(); self.refresh(); self.parent.refresh_operation_controls()


class MovementDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp", movement: sqlite3.Row):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent, self.movement, self.result = parent, movement, None
        self.product_mapping = parent.product_map()
        self.operation_mapping = parent.operation_map(include_inactive=True)
        self.title("Editar movimentação")
        scale = parent.ui_scale
        width, height = round(520 * scale), round(670 * scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+100}+{parent.winfo_y()+70}")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="HISTÓRICO", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=28, pady=(22, 2))
        ctk.CTkLabel(header, text="Editar movimentação", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 23, "bold")).pack(anchor="w", padx=28, pady=(0, 22))
        form = SmoothScrollableFrame(self, fg_color="transparent", corner_radius=0)
        form.grid(row=1, column=0, sticky="nsew", padx=24, pady=20)

        def label(text):
            ctk.CTkLabel(form, text=text, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).pack(anchor="w", pady=(0, 6))

        selected_operation = next((name for name, operation_id in self.operation_mapping.items() if operation_id == int(movement["operation_id"] or 0)), movement["operation_name"])
        self.operation = tk.StringVar(value=selected_operation)
        label("Operação")
        ctk.CTkOptionMenu(form, variable=self.operation, values=list(self.operation_mapping) or [""], height=40, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"]).pack(fill="x", pady=(0, 16))

        selected_product = next((text for text, product_id in self.product_mapping.items() if product_id == int(movement["product_id"])), "")
        self.product = tk.StringVar(value=selected_product)
        label("Produto")
        ctk.CTkOptionMenu(form, variable=self.product, values=list(self.product_mapping) or [""], height=40, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"]).pack(fill="x", pady=(0, 16))

        informed = abs(float(movement["quantity"])) if movement["type"] in ("entrada", "saida") else float(movement["informed_quantity"] if movement["informed_quantity"] is not None else movement["resulting_stock"])
        self.quantity = tk.StringVar(value=fmt_number(informed))
        label("Quantidade / nova contagem")
        ctk.CTkEntry(form, textvariable=self.quantity, height=40, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"]).pack(fill="x", pady=(0, 16))

        label("Data (DD/MM/AA)")
        self.date_entry = MaskedDateEntry(form, COLORS, initial=datetime.strptime(movement["movement_date"], "%Y-%m-%d").date())
        self.date_entry.pack(fill="x", pady=(0, 16))

        self.checked_by = tk.StringVar(value=movement["checked_by"] or "")
        label("Usuário responsável")
        user_values=parent.user_names()
        if self.checked_by.get() and self.checked_by.get() not in user_values:user_values.append(self.checked_by.get())
        ctk.CTkOptionMenu(form,variable=self.checked_by,values=user_values or [""],height=40,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"]).pack(fill="x",pady=(0,16))

        reason = movement["reason"] if movement["reason"] not in ("Sem observação", "Contagem de inventário") else ""
        self.reason = tk.StringVar(value=reason)
        label("Motivo ou observação")
        ctk.CTkEntry(form, textvariable=self.reason, height=40, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"]).pack(fill="x", pady=(0, 22))

        actions = ctk.CTkFrame(form, fg_color="transparent"); actions.pack(fill="x")
        ctk.CTkButton(actions, text="Cancelar", width=120, height=42, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(actions, text="Salvar alterações", width=160, height=42, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.save).pack(side="right")

    def save(self):
        product_id = self.product_mapping.get(self.product.get())
        if not product_id:
            messagebox.showwarning(APP_NAME, "Selecione um produto.", parent=self); return
        try:
            informed = float(self.quantity.get().replace(",", "."))
            movement_date = self.date_entry.get_date().isoformat()
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error) or "Revise a quantidade e a data.", parent=self); return
        operation_id = self.operation_mapping.get(self.operation.get())
        if not operation_id:
            messagebox.showwarning(APP_NAME, "Selecione uma operação.", parent=self); return
        if not self.checked_by.get().strip():
            messagebox.showwarning(APP_NAME, "Informe o usuário responsável.", parent=self); return
        self.result = {"product_id": product_id, "operation": operation_id, "informed": informed, "movement_date": movement_date, "reason": self.reason.get().strip(), "checked_by": self.checked_by.get().strip()}
        self.destroy()


class MovementHistoryDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp", history_key: str):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent, self.history_key, self.action = parent, history_key, None
        kind, raw_id = history_key.split(":", 1)
        self.record_id = int(raw_id)
        if kind == "batch":
            record = parent.db.movement_batch(self.record_id)
            items = parent.db.movement_batch_items(self.record_id)
        else:
            movement = parent.db.movement(self.record_id)
            record = movement
            items = [movement] if movement else []
        if not record or not items:
            self.destroy()
            raise ValueError("Essa movimentação não existe mais.")

        self.title("Detalhes da movimentação")
        scale = parent.ui_scale
        width, height = round(880 * scale), round(610 * scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+70}+{parent.winfo_y()+55}")
        self.minsize(round(720 * scale), round(510 * scale))
        self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="HISTÓRICO DE MOVIMENTAÇÕES", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", padx=28, pady=(20, 2))
        ctk.CTkLabel(header, text=f"Movimentação #{self.record_id}", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 23, "bold")).pack(anchor="w", padx=28)
        item_word = "produto" if len(items) == 1 else "produtos"
        ctk.CTkLabel(header, text=f"Conjunto fechado com {len(items)} {item_word}.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 11)).pack(anchor="w", padx=28, pady=(5, 18))

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=24, pady=20)
        content.grid_columnconfigure(0, weight=1); content.grid_rowconfigure(1, weight=1)

        summary = Card(content); summary.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        summary.grid_columnconfigure((0, 1, 2), weight=1)
        movement_date = datetime.strptime(record["movement_date"], "%Y-%m-%d").strftime("%d/%m/%y")
        operation_name = record["operation_name"]
        responsible = record["performed_by"] if "performed_by" in record.keys() else record["checked_by"]
        for column, (label, value) in enumerate((("Data", movement_date), ("Operação", operation_name), ("Usuário responsável", responsible or "—"))):
            box = ctk.CTkFrame(summary, fg_color="transparent"); box.grid(row=0, column=column, sticky="ew", padx=18, pady=(15, 8))
            ctk.CTkLabel(box, text=label, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 9, "bold")).pack(anchor="w")
            ctk.CTkLabel(box, text=value, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 13, "bold")).pack(anchor="w", pady=(3, 0))
        reason = record["reason"] or "Sem observação"
        ctk.CTkLabel(summary, text=f"Observação: {reason}", wraplength=round(780 * scale), justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).grid(row=1, column=0, columnspan=3, sticky="w", padx=18, pady=(0, 15))

        listing = Card(content); listing.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(listing, text="Produtos incluídos", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 15, "bold")).pack(anchor="w", padx=18, pady=(16, 10))
        self.tree = parent.table(listing, ("product", "informed", "change", "stock"), ("Produto", "Quantidade informada", "Alteração", "Saldo após"), (360, 145, 120, 120))
        self.tree.column("product", anchor="w")
        self.tree.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        for item in items:
            quantity = float(item["quantity"])
            informed = abs(quantity) if item["type"] in ("entrada", "saida") else float(item["informed_quantity"] if item["informed_quantity"] is not None else item["resulting_stock"])
            self.tree.insert("", "end", values=(product_label(item), fmt_number(informed), f"{'+' if quantity > 0 else ''}{fmt_number(quantity)} {item['unit']}", f"{fmt_number(item['resulting_stock'])} {item['unit']}"))

        actions = ctk.CTkFrame(content, fg_color="transparent"); actions.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ctk.CTkButton(actions, text="Fechar", width=100, height=40, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.destroy).pack(side="left")
        ctk.CTkButton(actions, text="Excluir movimentação", image=parent.icons["trash"], width=175, height=40, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["danger"], command=lambda:self.finish("delete")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(actions, text="Editar movimentação", image=parent.icons["edit"], width=170, height=40, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=lambda:self.finish("edit")).pack(side="right")

    def finish(self, action: str):
        self.action = action
        self.destroy()


class CloudLoginDialog(BrandedToplevel):
    def __init__(self, parent: "EstoqueApp"):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent = parent
        self.title("Entrar no Supabase")
        self.geometry(f"480x410+{parent.winfo_x()+180}+{parent.winfo_y()+100}")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text="SINCRONIZAÇÃO SEGURA", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold")).grid(row=0,column=0,sticky="w",padx=32,pady=(30,4))
        ctk.CTkLabel(self, text="Sua conta na nuvem", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 23, "bold")).grid(row=1,column=0,sticky="w",padx=32)
        ctk.CTkLabel(self, text="Use um e-mail e uma senha exclusivos para proteger o estoque.\nA senha não fica salva no aplicativo.", justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 11)).grid(row=2,column=0,sticky="w",padx=32,pady=(7,22))
        self.email = ctk.CTkEntry(self, placeholder_text="E-mail", height=42)
        self.email.grid(row=3,column=0,sticky="ew",padx=32,pady=6);self.email.insert(0,parent.cloud.email)
        self.password = ctk.CTkEntry(self, placeholder_text="Senha (mínimo de 6 caracteres)", show="•", height=42)
        self.password.grid(row=4,column=0,sticky="ew",padx=32,pady=6)
        actions=ctk.CTkFrame(self,fg_color="transparent");actions.grid(row=5,column=0,sticky="ew",padx=32,pady=(18,8));actions.grid_columnconfigure((0,1),weight=1)
        ctk.CTkButton(actions,text="Criar conta",height=42,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.sign_up).grid(row=0,column=0,sticky="ew",padx=(0,6))
        ctk.CTkButton(actions,text="Entrar",height=42,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.sign_in).grid(row=0,column=1,sticky="ew",padx=(6,0))
        self.password.bind("<Return>",lambda _event:self.sign_in());self.email.focus_set()

    def credentials(self):
        email,password=self.email.get().strip(),self.password.get()
        if "@" not in email or len(password)<6:
            messagebox.showwarning(APP_NAME,"Informe um e-mail válido e uma senha com pelo menos 6 caracteres.",parent=self);return None
        return email,password

    def sign_in(self):
        credentials=self.credentials()
        if not credentials:return
        try:self.parent.cloud.sign_in(*credentials);self.parent.save_cloud_settings()
        except CloudSyncError as error:messagebox.showerror(APP_NAME,str(error),parent=self);return
        self.destroy();self.parent.update_cloud_status();self.parent.start_cloud_sync(silent=False,prefer_local=True)

    def sign_up(self):
        credentials=self.credentials()
        if not credentials:return
        try:signed_in=self.parent.cloud.sign_up(*credentials);self.parent.save_cloud_settings()
        except CloudSyncError as error:messagebox.showerror(APP_NAME,str(error),parent=self);return
        if signed_in:
            self.destroy();self.parent.update_cloud_status();self.parent.start_cloud_sync(silent=False,prefer_local=True)
        else:
            messagebox.showinfo(APP_NAME,"Conta criada. Confirme o e-mail recebido e depois use o botão Entrar.",parent=self)


class EstoqueApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=COLORS["background"])
        self.withdraw()
        dpi = float(self.winfo_fpixels("1i")); self.ui_scale = max(1, min(dpi/96, 3)); self.tk.call("tk", "scaling", dpi/72)
        legacy_settings = read_json_object(data_dir()/"settings.json")
        self.preferences_store = LocalPreferences(data_dir()/"ui-preferences.json", legacy_settings)
        self.cloud_session_store = LocalCloudSession(data_dir()/"cloud-session.json", legacy_settings)
        self.simulation_store = LocalSimulationDraft(data_dir()/"simulation-draft.json")
        self.settings = self.preferences_store.values
        self.cloud_settings = self.cloud_session_store.values
        ctk.set_appearance_mode(self.settings.get("theme", "Light"))
        self.db = Database(); self.cloud = CloudSync(data_dir(), self.cloud_settings); self.db.on_change = self.schedule_cloud_sync; self.save_cloud_settings(); self.title(f"{APP_NAME} — v{APP_VERSION}")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight(); self.work_areas = monitor_work_areas(sw, sh)
        primary = self.work_areas[0]; work_width, work_height = primary[2]-primary[0], primary[3]-primary[1]
        self.minimum_width = min(work_width, round(1050*self.ui_scale)); self.minimum_height = min(work_height, round(680*self.ui_scale))
        self.minsize(self.minimum_width, self.minimum_height)
        self._normal_geometry = visible_window_geometry(self.settings.get("window_geometry"), self.work_areas, self.minimum_width, self.minimum_height)
        self.geometry(self._normal_geometry)
        self._last_window_state = self.settings.get("window_state", "zoomed") if self.settings.get("window_state") in ("normal", "zoomed") else "zoomed"
        apply_window_icon(self); self.protocol("WM_DELETE_WINDOW", self.close)
        self.brand_icon = brand_mark(86)
        self.icons = {name: icon(name, 22) for name in ("products", "stock", "movements", "defect_return", "kit_conversion", "simulation", "count", "settings", "registration", "user", "operation", "group", "plus", "search", "edit", "trash", "download", "upload", "refresh", "print", "collapse", "expand")}
        self.table_separators: list[TreeRowSeparatorOverlay] = []
        self._ui_jobs: dict[str, str] = {}
        self.update_events: queue.Queue = queue.Queue(); self.update_busy = False; self.update_button = None
        self.cloud_events: queue.Queue = queue.Queue(); self.cloud_sync_busy = False; self.cloud_sync_pending = False; self.cloud_sync_timer = None
        self.nav_buttons = {}; self.pages = {}; self.current_page = ""; self.build_shell(); self.show_page(self.settings.get("last_page", "stock"))
        self.bind("<Configure>", self.remember_window_geometry)
        self.after_idle(self.restore_window)
        self.after(2500, lambda: self.check_updates(silent=True))
        self.after(4000, lambda: self.start_cloud_sync(silent=True))
        self.after(20000, self.periodic_cloud_sync)

    def save_settings(self): self.preferences_store.save(); self.settings = self.preferences_store.values

    def schedule_ui_task(self, key: str, callback, delay: int = 120):
        """Coalesce repeated UI events so typing and navigation stay responsive."""
        previous = self._ui_jobs.pop(key, None)
        if previous is not None:
            try:self.after_cancel(previous)
            except (tk.TclError, ValueError):pass

        def run():
            self._ui_jobs.pop(key, None)
            callback()

        self._ui_jobs[key] = self.after(delay, run)

    def cancel_ui_task(self, key: str):
        job = self._ui_jobs.pop(key, None)
        if job is not None:
            try:self.after_cancel(job)
            except (tk.TclError, ValueError):pass

    def schedule_settings_save(self):
        self.schedule_ui_task("save_settings", self.save_settings, 260)

    def save_cloud_settings(self): self.cloud_session_store.values = self.cloud_settings; self.cloud_session_store.save(); self.cloud_settings = self.cloud_session_store.values

    def capture_interface_preferences(self):
        if self.current_page:self.settings["last_page"] = self.current_page
        if hasattr(self,"stock_search"):self.settings["stock_search"] = self.stock_search.get()
        if hasattr(self,"count_search"):self.settings["count_search"] = self.count_search.get()
        if hasattr(self,"count_filter"):self.settings["count_filter"] = self.count_filter.get()
        if hasattr(self,"count_product_suggestions_collapsed"):self.settings["count_products_expanded"] = not self.count_product_suggestions_collapsed
        if hasattr(self,"m_operation"):self.settings["movement_operation"] = self.m_operation.get()
        if hasattr(self,"m_user"):self.settings["movement_user"] = self.m_user.get()
        if hasattr(self,"quick_action"):self.settings["quick_stock_action"] = self.quick_action.get()
        if hasattr(self,"quick_user"):self.settings["quick_stock_user"] = self.quick_user.get()
        if hasattr(self,"kit_mode"):self.settings["kit_conversion_mode"] = self.kit_mode.get()
        if hasattr(self,"kit_user"):self.settings["kit_conversion_user"] = self.kit_user.get()
        if hasattr(self,"history_filter"):self.settings["history_filter"] = self.history_filter.get()
        if hasattr(self,"product_suggestions_collapsed"):self.settings["movement_products_expanded"] = not self.product_suggestions_collapsed

    def save_interface_state(self):self.capture_interface_preferences();self.schedule_settings_save()

    def restore_window(self):
        self.update_idletasks()
        self.deiconify()
        if self._last_window_state == "zoomed":
            try: self.state("zoomed")
            except tk.TclError: self.geometry(self._normal_geometry)
        else:
            self.state("normal"); self.geometry(self._normal_geometry)
        self.after(80, self.lift)

    def remember_window_geometry(self, _event=None):
        try: state = self.state()
        except tk.TclError: return
        if state == "zoomed":
            self._last_window_state = "zoomed"
        elif state == "normal" and self.winfo_width() >= 300 and self.winfo_height() >= 240:
            self._last_window_state = "normal"
            geometry = self.geometry()
            if parse_window_geometry(geometry): self._normal_geometry = geometry

    def build_shell(self):
        self.grid_columnconfigure(1, weight=1); self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=265, corner_radius=0, fg_color=COLORS["sidebar"]); self.sidebar.grid(row=0, column=0, sticky="nsw"); self.sidebar.grid_propagate(False)
        logo = ctk.CTkFrame(self.sidebar, fg_color="transparent"); logo.pack(fill="x", padx=24, pady=(28, 34))
        ctk.CTkLabel(logo, text="", image=self.brand_icon, width=86, height=46).pack(side="left")
        brand = ctk.CTkFrame(logo, fg_color="transparent"); brand.pack(side="left", padx=13)
        ctk.CTkLabel(brand, text="ESTOQUE", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(brand, text="BOLSAS BABY", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", pady=(2,0))
        for key, label in (("stock","Estoque atual"),("movements","Movimentações"),("defect_return","Defeito / Devolução"),("kit_conversion","Montagem / Desmontagem"),("simulation","Simulação"),("count","Contagem"),("registration","Cadastro"),("settings","Configurações")):
            font_size = 11 if key in ("defect_return", "kit_conversion") else 13
            button = ctk.CTkButton(self.sidebar, text=label, image=self.icons[key], compound="left", anchor="w", height=48, corner_radius=10, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], font=ctk.CTkFont("Inter", font_size, "bold"), command=lambda k=key:self.show_page(k))
            button.pack(fill="x", padx=16, pady=4); self.nav_buttons[key]=button
        self.sidebar_status=ctk.CTkLabel(self.sidebar, text=f"●  Local + nuvem segura\n    Versão {APP_VERSION}", justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10));self.sidebar_status.pack(side="bottom", anchor="w", padx=26, pady=28)
        self.content = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0); self.content.grid(row=0,column=1,sticky="nsew"); self.content.grid_columnconfigure(0,weight=1); self.content.grid_rowconfigure(0,weight=1)

    def show_page(self,key):
        if key == self.current_page and key in self.pages:return
        self.capture_interface_preferences()
        for page in self.pages.values(): page.grid_remove()
        if key not in self.pages: self.pages[key]={"registration":self.registration_page,"stock":self.stock_page,"movements":self.movements_page,"defect_return":self.defect_return_page,"kit_conversion":self.kit_conversion_page,"simulation":self.simulation_page,"count":self.count_page,"settings":self.settings_page}[key]()
        self.pages[key].grid(row=0,column=0,sticky="nsew",padx=32,pady=28)
        self.current_page=key;self.settings["last_page"]=key;self.schedule_settings_save()
        for name,button in self.nav_buttons.items():
            selected = name == key
            button.configure(
                fg_color=COLORS["nav_selected"] if selected else "transparent",
                text_color=COLORS["accent"] if selected else COLORS["muted"],
                border_width=1 if selected else 0,
                border_color=COLORS["accent"] if selected else COLORS["sidebar"],
            )
        refresh={"registration":lambda:None,"stock":self.refresh_stock,"movements":self.refresh_movement_page,"defect_return":self.refresh_defect_return_page,"kit_conversion":self.refresh_kit_conversion,"simulation":self.refresh_simulation_page,"count":self.refresh_counts,"settings":lambda:None}[key]
        self.schedule_ui_task("page_refresh",lambda:refresh() if self.current_page==key else None,1)

    def table(self,parent,columns,headings,widths):
        tree=ttk.Treeview(parent,columns=columns,show="headings",selectmode="browse")
        for col,label,width in zip(columns,headings,widths,strict=True): tree.heading(col,text=label,anchor="center"); tree.column(col,width=width,anchor="center")
        separator=TreeRowSeparatorOverlay(tree,COLORS);tree._row_separator=separator;self.table_separators.append(separator)
        original_insert,original_delete=tree.insert,tree.delete
        def insert_with_separator(*args,**kwargs):
            item=original_insert(*args,**kwargs);separator.schedule();return item
        def delete_with_separator(*args,**kwargs):
            result=original_delete(*args,**kwargs);separator.schedule();return result
        tree.insert=insert_with_separator;tree.delete=delete_with_separator
        def scroll_tree(event):
            units=tree_wheel_units(event.delta)
            if units:tree.yview_scroll(units,"units");tree.event_generate("<<TreeViewportChanged>>");separator.schedule()
            return "break"
        tree.bind("<MouseWheel>",scroll_tree,add="+")
        return tree

    def configure_tables(self):
        dark=ctk.get_appearance_mode()=="Dark"; bg="#121824" if dark else "#FFFFFF"; fg="#F3F7FB" if dark else "#202936"; head="#192232" if dark else "#EEF3F8"; selected="#203C52" if dark else "#DDEFFC"
        style=ttk.Style(self); style.theme_use("clam"); style.configure("Treeview",background=bg,fieldbackground=bg,foreground=fg,rowheight=max(38,round(34*self.ui_scale)),borderwidth=0,font=("Inter",10)); style.configure("Treeview.Heading",background=head,foreground=fg,relief="flat",font=("Inter",9,"bold"),padding=10); style.map("Treeview",background=[("selected",selected)],foreground=[("selected",fg)])
        if hasattr(self,"stock_tree"):self.stock_tree.tag_configure("group_header",background="#17293B" if dark else "#E4F0F7",foreground="#8BD5FF" if dark else "#245F89",font=("Inter",10,"bold"))
        active_separators=[]
        for separator in getattr(self,"table_separators",[]):
            try:
                if separator.tree.winfo_exists():separator.schedule();active_separators.append(separator)
            except tk.TclError:
                continue
        self.table_separators=active_separators

    def registration_page(self):
        page=SmoothScrollableFrame(self.content,fg_color="transparent",corner_radius=0,scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        PageTitle(page,"Cadastro","Escolha o tipo de cadastro que deseja abrir.").pack(fill="x",pady=(0,26))
        ctk.CTkLabel(page,text="Cada opção possui seu próprio gerenciador, incluindo a memória dos SKUs usados nas listas de vendas.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",pady=(0,18))
        choices=ctk.CTkFrame(page,fg_color="transparent");choices.pack(fill="both",expand=True);choices.grid_columnconfigure((0,1),weight=1);choices.grid_rowconfigure((0,1,2),weight=1)
        definitions=(
            ("Usuários","Cadastre quem poderá ser identificado nas movimentações.","user",self.open_user_manager),
            ("Operações","Defina nomes personalizados que somam ou retiram estoque.","operation",self.open_operation_manager),
            ("Grupos","Organize modelos como 2 PEÇAS, 4 PEÇAS e outras famílias.","group",self.open_group_manager),
            ("Produtos","Cadastre, edite e organize os produtos e seus grupos.","products",self.open_product_manager),
            ("SKUs de venda","Consulte e altere quais produtos cada SKU descontará nas próximas listas.","registration",self.open_sku_manager),
        )
        for index,(title,description,icon_name,command) in enumerate(definitions):
            row,column=divmod(index,2);card=Card(choices);card.grid(row=row,column=column,sticky="nsew",padx=(0 if column==0 else 8,8 if column==0 else 0),pady=(0 if row==0 else 8,8 if row==0 else 0))
            large_icon=icon(icon_name,54)
            label=ctk.CTkLabel(card,text="",image=large_icon,width=72,height=66);label.image=large_icon;label.pack(pady=(20,7))
            ctk.CTkLabel(card,text=title,text_color=COLORS["text"],font=ctk.CTkFont("Inter",17,"bold")).pack()
            ctk.CTkLabel(card,text=description,wraplength=330,justify="center",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(padx=22,pady=(6,12))
            ctk.CTkButton(card,text=f"Abrir cadastro de {title.lower()}",height=40,corner_radius=10,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=command).pack(fill="x",padx=24,pady=(0,20))
        return page

    def open_user_manager(self):
        dialog=UserManagerDialog(self);self.wait_window(dialog);self.refresh_user_controls()
    def open_group_manager(self):
        dialog=GroupManagerDialog(self);self.wait_window(dialog);self.refresh_all()
    def open_product_manager(self):
        dialog=ProductManagerDialog(self);self.wait_window(dialog);self.refresh_all()
    def open_sku_manager(self):
        dialog=SkuManagerDialog(self);self.wait_window(dialog)

    def products_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent"); PageTitle(page,"Produtos","Cadastre e organize os itens do seu estoque.").pack(fill="x",pady=(0,22))
        toolbar=ctk.CTkFrame(page,fg_color="transparent");toolbar.pack(fill="x",pady=(0,16)); self.product_search=ctk.CTkEntry(toolbar,placeholder_text="Buscar por produto, grupo ou variação...",width=430,height=44,corner_radius=10,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.product_search.pack(side="left");self.product_search.bind("<KeyRelease>",lambda _event:self.schedule_ui_task("products_search",self.refresh_products,100))
        for text,name,cmd,color in (("Novo produto","plus",self.new_product,COLORS["accent"]),("Editar","edit",self.edit_product,COLORS["surface_alt"]),("Excluir","trash",self.delete_product,COLORS["surface_alt"])):
            ctk.CTkButton(toolbar,text=text,image=self.icons[name],height=44,corner_radius=10,fg_color=color,hover_color=COLORS["accent_hover"] if name=="plus" else COLORS["surface_hover"],text_color="#FFFFFF" if name=="plus" else COLORS["text"],command=cmd).pack(side="left",padx=(10,0))
        card=Card(page);card.pack(fill="both",expand=True); self.product_tree=self.table(card,("name","group","variant","category","unit","minimum","stock"),("Produto","Grupo / modelo","Variação","Categoria","Un.","Mínimo","Saldo"),(200,180,165,140,55,80,80));self.product_tree.pack(fill="both",expand=True,padx=20,pady=20);self.product_tree.bind("<Double-1>",lambda e:self.edit_product());self.configure_tables();return page

    def refresh_products(self):
        if not hasattr(self,"product_tree"):return
        self.product_tree.delete(*self.product_tree.get_children()); search=self.product_search.get() if hasattr(self,"product_search") else ""
        for p in self.db.products(search):self.product_tree.insert("","end",iid=str(p["id"]),values=(p["name"],p["group_name"]or"—",p["variant"]or"—",p["category"]or"—",p["unit"],fmt_number(p["minimum"]),fmt_number(p["stock"])))

    def selected_product(self):
        selected=self.product_tree.selection();return int(selected[0]) if selected else None

    def new_product(self):
        dialog=ProductDialog(self);self.wait_window(dialog)
        if not dialog.result:return
        try:self.db.save_product(dialog.result)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all()

    def edit_product(self):
        pid=self.selected_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para editar.",parent=self);return
        dialog=ProductDialog(self,self.db.product(pid));self.wait_window(dialog)
        if not dialog.result:return
        try:self.db.save_product(dialog.result,pid)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all()

    def delete_product(self):
        pid=self.selected_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para excluir.",parent=self);return
        product=self.db.product(pid)
        if messagebox.askyesno(APP_NAME,f"Excluir o produto “{product['name']}”?",parent=self):
            if not self.db.delete_product(pid):messagebox.showwarning(APP_NAME,"Produtos com histórico não podem ser excluídos.",parent=self)
            self.refresh_all()

    def stock_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Estoque atual","Uma visão clara dos saldos e itens que precisam de atenção.").pack(fill="x",pady=(0,22))
        self.negative_stock_alert=ctk.CTkFrame(page,fg_color=("#F6E7EA","#3A0711"),corner_radius=12,border_width=2,border_color=("#5A0B1A","#8F2433"))
        ctk.CTkLabel(self.negative_stock_alert,text="ESTOQUE NEGATIVO",text_color=("#5A0B1A","#FFB3BE"),font=ctk.CTkFont("Inter",13,"bold")).pack(anchor="w",padx=18,pady=(14,3))
        self.negative_stock_alert_text=ctk.CTkLabel(self.negative_stock_alert,text="",justify="left",anchor="w",wraplength=940,text_color=("#5A0B1A","#FFF1F3"),font=ctk.CTkFont("Inter",11,"bold"));self.negative_stock_alert_text.pack(fill="x",padx=18,pady=(0,14))
        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,16));self.stock_cards_container=cards;self.stock_cards=[]
        for title in ("Produtos","Unidades em estoque","Abaixo do mínimo","Confiança baixa"):
            card=Card(cards,height=108);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=18,pady=(17,3));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold"));label.pack(anchor="w",padx=18);self.stock_cards.append(label)
        card=Card(page);card.pack(fill="both",expand=True);bar=ctk.CTkFrame(card,fg_color="transparent");bar.pack(fill="x",padx=20,pady=(18,8));ctk.CTkLabel(bar,text="Posição do estoque",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(side="left");self.stock_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto ou grupo...",width=300,height=38,corner_radius=9);self.stock_search.pack(side="right");self.stock_search.insert(0,self.settings.get("stock_search",""));self.stock_search.bind("<KeyRelease>",lambda _event:self.schedule_ui_task("stock_search",lambda:(self.refresh_stock(),self.save_interface_state()),100))
        stock_table=ctk.CTkFrame(card,fg_color="transparent");stock_table.pack(fill="both",expand=True,padx=20,pady=(8,20));stock_table.grid_columnconfigure(0,weight=1);stock_table.grid_rowconfigure(0,weight=1)
        self.stock_tree=self.table(stock_table,("group","name","stock","confidence"),("Grupo / modelo","Produto","Saldo atual","Confiança"),(220,320,140,140));self.stock_tree.grid(row=0,column=0,sticky="nsew")
        self.stock_scrollbar=ctk.CTkScrollbar(stock_table,orientation="vertical",command=self.stock_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);self.stock_scrollbar.grid(row=0,column=1,sticky="ns",padx=(8,0));self.stock_tree.configure(yscrollcommand=self.stock_scrollbar.set)
        self.stock_confidence_cells=TreeConfidenceOverlay(self.stock_tree,COLORS);self.stock_quantity_cells=TreeStockOverlay(self.stock_tree,COLORS);self.configure_tables();return page

    def refresh_stock(self):
        if not hasattr(self,"stock_tree"):return
        negative_products=self.db.negative_stock_products()
        if negative_products:
            negative_details="  •  ".join(f"{product_label(product)}: {fmt_number(product['stock'])} {product['unit']}" for product in negative_products)
            self.negative_stock_alert_text.configure(text=f"{negative_details}\nVerifique as movimentações e registre uma entrada ou um ajuste positivo para corrigir o saldo.")
            if not self.negative_stock_alert.winfo_manager():self.negative_stock_alert.pack(fill="x",pady=(0,16),before=self.stock_cards_container)
        elif self.negative_stock_alert.winfo_manager():self.negative_stock_alert.pack_forget()
        items=self.db.products(self.stock_search.get() if hasattr(self,"stock_search") else "");confidence=self.db.stock_confidences(items);self.stock_tree.delete(*self.stock_tree.get_children());units=low=low_confidence=0;scores={};quantities={};current_group=None;group_index=0
        for p in items:
            group=(p["group_name"]or"").strip()or"Sem grupo";group_key=group.casefold()
            if group_key!=current_group:group_index+=1;current_group=group_key;self.stock_tree.insert("","end",iid=f"group:{group_index}",values=(f"—  {group.upper()}  —","","",""),tags=("group_header",))
            stock=float(p["stock"]);units+=stock;status="Negativo" if stock<0 else "Sem estoque" if stock==0 else "Estoque baixo" if stock<=float(p["minimum"]) else "Normal";low+=status!="Normal";trust=confidence[int(p["id"])];low_confidence+=trust["level"]=="Baixa";scores[int(p["id"])]=trust["score"];quantities[int(p["id"])]=(stock,fmt_number(stock));self.stock_tree.insert("","end",iid=str(p["id"]),values=("",p["name"],"",""))
        self.stock_confidence_cells.set_scores(scores)
        self.stock_quantity_cells.set_quantities(quantities)
        for label,text in zip(self.stock_cards,(str(len(items)),fmt_number(units),str(low),str(low_confidence)),strict=True):label.configure(text=text)

    def simulation_page(self):
        page=SmoothScrollableFrame(self.content,fg_color="transparent",corner_radius=0,scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        PageTitle(page,"Simulação","Planeje uma entrada ou saída e veja quanto restará sem alterar o estoque real.").pack(fill="x",pady=(0,16))

        notice=ctk.CTkFrame(page,fg_color=COLORS["accent_soft"],corner_radius=12,border_width=1,border_color=COLORS["accent"])
        notice.pack(fill="x",pady=(0,14))
        ctk.CTkLabel(notice,text="SIMULAÇÃO SEGURA",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",11,"bold")).pack(anchor="w",padx=18,pady=(12,2))
        ctk.CTkLabel(notice,text="Este rascunho fica salvo somente neste computador. Nada será registrado nas movimentações ou enviado ao Supabase.",text_color=COLORS["text"],font=ctk.CTkFont("Inter",10),anchor="w",wraplength=920).pack(fill="x",padx=18,pady=(0,12))

        stored=self.simulation_store.values
        self.simulation_items=[dict(item) for item in stored.get("items",[])]
        self.sim_selected_product_id: int|None=None
        self.sim_operation=tk.StringVar(value="Entrada" if stored.get("operation")=="entrada" else "Saída")
        self.sim_product=tk.StringVar();self.sim_quantity=tk.StringVar()

        controls=Card(page);controls.pack(fill="x",pady=(0,14));controls.grid_columnconfigure(1,weight=1)
        ctk.CTkLabel(controls,text="Tipo da operação",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).grid(row=0,column=0,sticky="w",padx=(20,12),pady=(18,6))
        ctk.CTkLabel(controls,text="Produto",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).grid(row=0,column=1,sticky="w",padx=8,pady=(18,6))
        ctk.CTkLabel(controls,text="Quantidade",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).grid(row=0,column=2,sticky="w",padx=8,pady=(18,6))
        self.sim_operation_control=ctk.CTkSegmentedButton(controls,variable=self.sim_operation,values=["Saída","Entrada"],width=190,height=40,corner_radius=9,selected_color=COLORS["accent"],selected_hover_color=COLORS["accent_hover"],unselected_color=COLORS["surface_alt"],unselected_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.on_simulation_operation_change)
        self.sim_operation_control.grid(row=1,column=0,sticky="ew",padx=(20,12),pady=(0,8))
        search=ctk.CTkFrame(controls,height=40,corner_radius=9,fg_color=COLORS["surface"],border_width=1,border_color=COLORS["border"]);search.grid(row=1,column=1,sticky="ew",padx=8,pady=(0,8));search.grid_columnconfigure(1,weight=1);search.grid_propagate(False)
        ctk.CTkLabel(search,text="",image=self.icons["search"],width=36).grid(row=0,column=0,sticky="nsew",padx=(6,0))
        self.sim_product_entry=ctk.CTkEntry(search,textvariable=self.sim_product,placeholder_text="Buscar produto, grupo ou variação...",height=34,corner_radius=0,border_width=0,fg_color="transparent")
        self.sim_product_entry.grid(row=0,column=1,sticky="nsew",padx=(0,4),pady=3);self.sim_product_entry.bind("<KeyRelease>",lambda _event:self.on_simulation_product_search());self.sim_product_entry.bind("<Return>",lambda _event:self.select_first_simulation_product())
        self.sim_quantity_entry=ctk.CTkEntry(controls,textvariable=self.sim_quantity,placeholder_text="Ex.: 25",width=120,height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"])
        self.sim_quantity_entry.grid(row=1,column=2,sticky="ew",padx=8,pady=(0,8));self.sim_quantity_entry.bind("<Return>",lambda _event:self.add_simulation_item())
        self.sim_add_button=ctk.CTkButton(controls,text="Adicionar ao conjunto",width=165,height=40,corner_radius=9,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.add_simulation_item)
        self.sim_add_button.grid(row=1,column=3,sticky="ew",padx=(8,20),pady=(0,8))
        self.sim_selected_stock=ctk.CTkLabel(controls,text="Selecione um produto para ver o saldo atual.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w")
        self.sim_selected_stock.grid(row=2,column=0,columnspan=4,sticky="ew",padx=20,pady=(0,8))
        self.sim_product_results=SmoothScrollableFrame(controls,height=112,corner_radius=9,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.sim_product_results.grid(row=3,column=0,columnspan=4,sticky="ew",padx=20,pady=(0,18))

        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,14));self.simulation_cards=[]
        for title in ("Produtos simulados","Unidades na operação","Saldos projetados negativos"):
            card=Card(cards,height=88);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=16,pady=(13,2));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",20,"bold"));label.pack(anchor="w",padx=16);self.simulation_cards.append(label)

        self.simulation_negative_alert=ctk.CTkFrame(page,fg_color=("#F6E7EA","#3A0711"),corner_radius=12,border_width=2,border_color=("#5A0B1A","#8F2433"))
        ctk.CTkLabel(self.simulation_negative_alert,text="ATENÇÃO: SALDO PROJETADO NEGATIVO",text_color=("#5A0B1A","#FFB3BE"),font=ctk.CTkFont("Inter",12,"bold")).pack(anchor="w",padx=18,pady=(12,2))
        self.simulation_negative_text=ctk.CTkLabel(self.simulation_negative_alert,text="",justify="left",anchor="w",wraplength=920,text_color=("#5A0B1A","#FFF1F3"),font=ctk.CTkFont("Inter",10,"bold"));self.simulation_negative_text.pack(fill="x",padx=18,pady=(0,12))

        self.simulation_result_card=Card(page);self.simulation_result_card.pack(fill="both",expand=True)
        result_bar=ctk.CTkFrame(self.simulation_result_card,fg_color="transparent");result_bar.pack(fill="x",padx=20,pady=(16,10))
        ctk.CTkLabel(result_bar,text="Produtos da simulação — estoque atual x simulado",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left")
        ctk.CTkButton(result_bar,text="Limpar simulação",width=125,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.clear_simulation).pack(side="right")
        self.sim_print_button=ctk.CTkButton(result_bar,text="Imprimir lista",image=self.icons["print"],width=120,height=34,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.print_simulation);self.sim_print_button.pack(side="right",padx=(0,6))
        ctk.CTkButton(result_bar,text="Remover",image=self.icons["trash"],width=100,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.remove_simulation_item).pack(side="right",padx=(0,6))
        ctk.CTkButton(result_bar,text="Editar",image=self.icons["edit"],width=92,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_simulation_item).pack(side="right",padx=(0,6))
        self.simulation_empty_label=ctk.CTkLabel(self.simulation_result_card,text="Adicione produtos acima para montar o conjunto da simulação.",height=38,corner_radius=9,fg_color=COLORS["surface_alt"],text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11))
        simulation_table=ctk.CTkFrame(self.simulation_result_card,fg_color="transparent");self.simulation_table=simulation_table;simulation_table.pack(fill="both",expand=True,padx=20,pady=(0,20));simulation_table.grid_columnconfigure(0,weight=1);simulation_table.grid_rowconfigure(0,weight=1)
        self.simulation_tree=self.table(simulation_table,("product","current","projected"),("Produto","Estoque atual","Simulado"),(420,190,190));self.simulation_tree.configure(height=10);self.simulation_tree.grid(row=0,column=0,sticky="nsew");self.simulation_tree.bind("<Double-1>",lambda _event:self.edit_simulation_item())
        self.simulation_scrollbar=ctk.CTkScrollbar(simulation_table,orientation="vertical",command=self.simulation_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);self.simulation_scrollbar.grid(row=0,column=1,sticky="ns",padx=(8,0));self.simulation_tree.configure(yscrollcommand=self.simulation_scrollbar.set)
        self.simulation_current_cells=TreeStockOverlay(self.simulation_tree,COLORS,column="current")
        self.simulation_projected_cells=TreeStockOverlay(self.simulation_tree,COLORS,column="projected")
        self.configure_tables()
        return page

    def refresh_simulation_page(self):
        self.refresh_simulation()
        self.schedule_ui_task("simulation_product_search",self.refresh_simulation_product_results,30)

    def simulation_operation_key(self):return "entrada" if self.sim_operation.get()=="Entrada" else "saida"
    def on_simulation_operation_change(self,_value=None):
        self.save_simulation_draft();self.refresh_simulation()
    def simulation_product_matches(self):
        query=self.sim_product.get() if hasattr(self,"sim_product") else ""
        selected=self.db.product(self.sim_selected_product_id) if getattr(self,"sim_selected_product_id",None) else None
        if selected and query==self.movement_product_display(selected):return [selected]
        return self.db.products(query)
    def on_simulation_product_search(self):
        selected=self.db.product(self.sim_selected_product_id) if self.sim_selected_product_id else None
        if not selected or self.sim_product.get()!=self.movement_product_display(selected):
            self.sim_selected_product_id=None;self.sim_selected_stock.configure(text="Selecione um produto para ver o saldo atual.",text_color=COLORS["muted"])
        self.schedule_ui_task("simulation_product_search",self.refresh_simulation_product_results,100)
    def refresh_simulation_product_results(self):
        if not hasattr(self,"sim_product_results"):return
        for child in self.sim_product_results.winfo_children():child.destroy()
        results=self.simulation_product_matches()
        if not results:
            ctk.CTkLabel(self.sim_product_results,text="Nenhum produto encontrado",height=36,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(fill="x",padx=8,pady=3);return
        for product in results[:SEARCH_RESULT_LIMIT]:
            current=float(product["stock"]);label=f"{product_label(product)}  •  Atual: {fmt_number(current)} {product['unit']}"
            selected=int(product["id"])==self.sim_selected_product_id
            ctk.CTkButton(self.sim_product_results,text=label,anchor="w",height=34,corner_radius=6,fg_color=COLORS["accent_soft"] if selected else "transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_simulation_product(product_id)).pack(fill="x",padx=5,pady=2)
        if len(results)>SEARCH_RESULT_LIMIT:ctk.CTkLabel(self.sim_product_results,text=f"Mostrando {SEARCH_RESULT_LIMIT} de {len(results)} produtos. Digite mais detalhes para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)
    def select_first_simulation_product(self):
        results=self.simulation_product_matches()
        if results:self.select_simulation_product(int(results[0]["id"]));self.sim_quantity_entry.focus_set()
    def select_simulation_product(self,product_id):
        self.cancel_ui_task("simulation_product_search")
        product=self.db.product(int(product_id))
        if not product:return
        self.sim_selected_product_id=int(product_id);self.sim_product.set(self.movement_product_display(product));self.sim_selected_stock.configure(text=f"Estoque atual: {fmt_number(product['stock'])} {product['unit']}",text_color=COLORS["accent"]);self.refresh_simulation_product_results()
    def add_simulation_item(self):
        product_id=self.sim_selected_product_id
        if not product_id:
            results=self.simulation_product_matches()
            if len(results)==1:product_id=int(results[0]["id"]);self.select_simulation_product(product_id)
            else:messagebox.showwarning(APP_NAME,"Escolha um produto na lista de resultados.",parent=self);return
        try:quantity=float(self.sim_quantity.get().replace(",","."))
        except (ValueError,OverflowError):messagebox.showwarning(APP_NAME,"Informe uma quantidade válida.",parent=self);return
        try:simulated_stock(0,quantity,self.simulation_operation_key())
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        item={"product_id":int(product_id),"quantity":quantity};existing=next((index for index,current in enumerate(self.simulation_items) if current["product_id"]==int(product_id)),None)
        if existing is None:self.simulation_items.append(item)
        else:self.simulation_items[existing]=item
        self.sim_selected_product_id=None;self.sim_product.set("");self.sim_quantity.set("");self.sim_selected_stock.configure(text="Selecione um produto para ver o saldo atual.",text_color=COLORS["muted"]);self.save_simulation_draft();self.refresh_simulation_product_results();self.refresh_simulation()
    def edit_simulation_item(self):
        selected=self.simulation_tree.selection()
        if not selected:messagebox.showinfo(APP_NAME,"Selecione um item da simulação para editar.",parent=self);return
        product_id=int(selected[0]);item=next((item for item in self.simulation_items if item["product_id"]==product_id),None)
        if not item:messagebox.showinfo(APP_NAME,"Esse produto aparece apenas para comparação e não foi incluído na simulação.",parent=self);return
        self.select_simulation_product(product_id);self.sim_quantity.set(fmt_number(item["quantity"]));self.sim_quantity_entry.focus_set()
    def remove_simulation_item(self):
        selected=self.simulation_tree.selection()
        if not selected:messagebox.showinfo(APP_NAME,"Selecione um item da simulação para remover.",parent=self);return
        product_id=int(selected[0])
        if not any(item["product_id"]==product_id for item in self.simulation_items):messagebox.showinfo(APP_NAME,"Esse produto aparece apenas para comparação e não foi incluído na simulação.",parent=self);return
        self.simulation_items=[item for item in self.simulation_items if item["product_id"]!=product_id];self.save_simulation_draft();self.refresh_simulation()
    def clear_simulation(self):
        if not self.simulation_items:return
        if messagebox.askyesno(APP_NAME,"Limpar todos os produtos desta simulação?",parent=self):self.simulation_items.clear();self.save_simulation_draft();self.refresh_simulation()
    def print_simulation(self):
        rows=simulation_selected_rows(self.db.products(),self.simulation_items,self.simulation_operation_key())
        if not rows:messagebox.showinfo(APP_NAME,"Adicione produtos à simulação antes de imprimir.",parent=self);return
        output=data_dir()/"impressoes"/f"lista-separacao-simulacao-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
        try:build_simulation_print_pdf(output,rows,self.simulation_operation_key());os.startfile(str(output))
        except (OSError,ValueError) as error:messagebox.showerror(APP_NAME,f"Não foi possível abrir a lista para impressão.\n\n{error}\n\nArquivo: {output}",parent=self)
    def print_current_stock(self):
        products=self.db.products()
        if not products:messagebox.showinfo(APP_NAME,"Cadastre produtos antes de imprimir o estoque atual.",parent=self);return
        output=data_dir()/"impressoes"/f"estoque-atual-conferencia-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
        try:build_current_stock_print_pdf(output,products);os.startfile(str(output))
        except (OSError,ValueError) as error:messagebox.showerror(APP_NAME,f"Não foi possível abrir o estoque para impressão.\n\n{error}\n\nArquivo: {output}",parent=self)
    def save_simulation_draft(self):
        if not hasattr(self,"simulation_items"):return
        self.simulation_store.values={"operation":self.simulation_operation_key(),"items":[dict(item) for item in self.simulation_items]};self.simulation_store.save()
    def refresh_simulation(self):
        if not hasattr(self,"simulation_tree"):return
        self.simulation_tree.delete(*self.simulation_tree.get_children());current_cells={};projected_cells={};negative=[];operation=self.simulation_operation_key();products=self.db.products();product_ids={int(product["id"]) for product in products}
        valid_items=[item for item in self.simulation_items if int(item["product_id"]) in product_ids]
        if len(valid_items)!=len(self.simulation_items):self.simulation_items=valid_items;self.save_simulation_draft()
        total=sum(float(item["quantity"]) for item in valid_items)
        rows=simulation_selected_rows(products,valid_items,operation)
        for row in rows:
            product=row["product"];product_id=row["product_id"];current=row["current"];quantity=row["quantity"];projected=row["projected"]
            self.simulation_tree.insert("","end",iid=str(product_id),values=(product_label(product),"",""));current_cells[product_id]=(current,f"{fmt_number(current)} {product['unit']}");projected_cells[product_id]=(projected,f"{fmt_number(projected)} {product['unit']}")
            if quantity is not None and projected<0:negative.append((product,projected))
        self.simulation_current_cells.set_quantities(current_cells);self.simulation_projected_cells.set_quantities(projected_cells)
        self.sim_print_button.configure(state="normal" if rows else "disabled")
        if rows:self.simulation_empty_label.pack_forget()
        else:self.simulation_empty_label.pack(fill="x",padx=20,pady=(0,8),before=self.simulation_table)
        for label,text in zip(self.simulation_cards,(str(len(valid_items)),fmt_number(total),str(len(negative))),strict=True):label.configure(text=text,text_color=("#5A0B1A","#FFB3BE") if label is self.simulation_cards[2] and negative else COLORS["text"])
        if negative:
            details="\n".join(f"• {product_label(product)}: {fmt_number(projected)} {product['unit']}" for product,projected in negative);self.simulation_negative_text.configure(text=details)
            if not self.simulation_negative_alert.winfo_manager():self.simulation_negative_alert.pack(fill="x",pady=(0,14),before=self.simulation_result_card)
        elif self.simulation_negative_alert.winfo_manager():self.simulation_negative_alert.pack_forget()

    def defect_return_page(self):
        page = SmoothScrollableFrame(
            self.content, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COLORS["accent"],
            scrollbar_button_hover_color=COLORS["accent_hover"],
        )
        PageTitle(
            page,
            "Defeito / Devolução",
            "Registre rapidamente uma única unidade e siga para o próximo produto.",
        ).pack(fill="x", pady=(0, 20))

        saved_action = self.settings.get("quick_stock_action", "Defeito")
        if saved_action not in QUICK_STOCK_ACTIONS:
            saved_action = "Defeito"
        users = self.user_names()
        saved_user = self.settings.get("quick_stock_user", "")
        selected_user = saved_user if saved_user in users else (users[0] if users else "Cadastre um usuário na aba Cadastro")
        self.quick_action = tk.StringVar(value=saved_action)
        self.quick_user = tk.StringVar(value=selected_user)
        self.quick_product = tk.StringVar()
        self.quick_selected_product_id: int | None = None
        self.quick_product_suggestions_collapsed = True

        action_card = Card(page); action_card.pack(fill="x", pady=(0, 16))
        action_content = ctk.CTkFrame(action_card, fg_color="transparent"); action_content.pack(fill="x", padx=22, pady=20)
        ctk.CTkLabel(action_content, text="Escolha a ação", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(action_content, text="Defeito retira 1 unidade. Devolução adiciona 1 unidade.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(anchor="w", pady=(4, 12))
        self.quick_action_selector = ctk.CTkSegmentedButton(
            action_content, variable=self.quick_action, values=list(QUICK_STOCK_ACTIONS),
            height=42, corner_radius=9, selected_color=COLORS["accent"],
            selected_hover_color=COLORS["accent_hover"], command=self.on_quick_action_change,
        )
        self.quick_action_selector.pack(fill="x"); self.quick_action_selector.set(saved_action)

        form = Card(page); form.pack(fill="x")
        content = ctk.CTkFrame(form, fg_color="transparent"); content.pack(fill="x", padx=22, pady=20)
        ctk.CTkLabel(content, text="Produto", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(content, text="Digite o grupo, o produto, a cor ou a variação.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(anchor="w", pady=(4, 10))
        self.quick_product_search = ctk.CTkFrame(content, height=46, corner_radius=10, fg_color=COLORS["surface"], border_width=2, border_color=COLORS["accent"])
        self.quick_product_search.pack(fill="x", pady=(0, 8)); self.quick_product_search.grid_columnconfigure(1, weight=1); self.quick_product_search.grid_propagate(False)
        ctk.CTkLabel(self.quick_product_search, text="", image=self.icons["search"], width=38).grid(row=0, column=0, sticky="nsew", padx=(7, 0), pady=4)
        self.quick_product_entry = ctk.CTkEntry(
            self.quick_product_search, textvariable=self.quick_product,
            placeholder_text="Buscar produto, grupo ou variação...", height=36,
            corner_radius=0, border_width=0, fg_color="transparent",
        )
        self.quick_product_entry.grid(row=0, column=1, sticky="nsew", padx=(0, 2), pady=3)
        self.quick_product_entry.bind("<FocusIn>", lambda _event:self.schedule_ui_task("quick_product_search", lambda:self.show_quick_product_suggestions(force=True), 50))
        self.quick_product_entry.bind("<KeyRelease>", self.on_quick_product_search)
        self.quick_product_entry.bind("<Return>", lambda _event:self.select_first_quick_product())
        self.quick_product_suggestions_toggle = ctk.CTkButton(
            self.quick_product_search, text="", image=self.icons["expand"], width=38, height=34,
            corner_radius=8, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"],
            command=self.toggle_quick_product_suggestions,
        )
        self.quick_product_suggestions_toggle.grid(row=0, column=2, sticky="e", padx=(2, 5), pady=4)
        self.quick_product_suggestions = SmoothScrollableFrame(
            content, height=205, corner_radius=9, fg_color=COLORS["surface_alt"],
            border_width=1, border_color=COLORS["border"],
            scrollbar_button_color=COLORS["accent"], scrollbar_button_hover_color=COLORS["accent_hover"],
        )

        info = ctk.CTkFrame(content, fg_color="transparent"); info.pack(fill="x", pady=(4, 12)); info.grid_columnconfigure((0, 1), weight=1)
        self.quick_current = ctk.CTkLabel(info, text="Saldo atual: —", height=40, corner_radius=9, fg_color=COLORS["accent_soft"], text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 11, "bold"))
        self.quick_current.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        user_box = ctk.CTkFrame(info, fg_color="transparent"); user_box.grid(row=0, column=1, sticky="ew", padx=(8, 0)); user_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(user_box, text="Responsável", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).grid(row=0, column=0, padx=(0, 10))
        self.quick_user_menu = ctk.CTkOptionMenu(
            user_box, variable=self.quick_user, values=users or [selected_user], height=40,
            corner_radius=9, fg_color=COLORS["surface_alt"], button_color=COLORS["surface_hover"],
            button_hover_color=COLORS["accent_soft"], text_color=COLORS["text"],
            dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"],
        )
        self.quick_user_menu.grid(row=0, column=1, sticky="ew")
        self.quick_register_button = ctk.CTkButton(
            content, text="", height=46, corner_radius=10,
            fg_color=COLORS["danger"], hover_color=COLORS["danger"],
            font=ctk.CTkFont("Inter", 13, "bold"), command=self.register_quick_stock_movement,
        )
        self.quick_register_button.pack(fill="x")
        self.quick_status = ctk.CTkLabel(
            content, text="Cada confirmação movimenta exatamente 1 unidade.",
            height=42, corner_radius=9, fg_color=COLORS["surface_alt"],
            text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10), wraplength=900,
        )
        self.quick_status.pack(fill="x", pady=(10, 0))
        self.on_quick_action_change(saved_action)
        self.after_idle(self.quick_product_entry.focus_set)
        return page

    def quick_product_results(self, query=""):
        return self.db.products(query)

    def hide_quick_product_suggestions(self):
        if hasattr(self, "quick_product_suggestions"):
            self.quick_product_suggestions.pack_forget(); self.quick_product_suggestions_collapsed = True
            self.quick_product_suggestions_toggle.configure(image=self.icons["expand"])

    def toggle_quick_product_suggestions(self):
        if self.quick_product_suggestions_collapsed:self.show_quick_product_suggestions(force=True);self.quick_product_entry.focus_set()
        else:self.hide_quick_product_suggestions()

    def show_quick_product_suggestions(self, force=False):
        if not hasattr(self, "quick_product_suggestions"):return
        if force:self.quick_product_suggestions_collapsed=False
        self.quick_product_suggestions_toggle.configure(image=self.icons["expand" if self.quick_product_suggestions_collapsed else "collapse"])
        if self.quick_product_suggestions_collapsed:
            self.quick_product_suggestions.pack_forget();return
        for child in self.quick_product_suggestions.winfo_children():child.destroy()
        results=self.quick_product_results(self.quick_product.get())
        if not results:
            ctk.CTkLabel(self.quick_product_suggestions,text="Nenhum produto encontrado",height=38,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(fill="x",padx=10,pady=4)
        else:
            for product in results[:SEARCH_RESULT_LIMIT]:
                label=f"{self.movement_product_display(product)}  •  Saldo: {fmt_number(product['stock'])} {product['unit']}"
                ctk.CTkButton(self.quick_product_suggestions,text=label,anchor="w",height=36,corner_radius=6,fg_color="transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_quick_product(product_id)).pack(fill="x",padx=5,pady=2)
            if len(results)>SEARCH_RESULT_LIMIT:ctk.CTkLabel(self.quick_product_suggestions,text=f"Mostrando {SEARCH_RESULT_LIMIT} de {len(results)}. Continue digitando para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)
        self.quick_product_suggestions.pack(fill="x",pady=(0,10),before=self.quick_current.master)

    def on_quick_product_search(self, event=None):
        if event and event.keysym in ("Return", "Escape"):
            if event.keysym=="Escape":self.hide_quick_product_suggestions()
            return
        selected=self.db.product(self.quick_selected_product_id) if self.quick_selected_product_id else None
        if not selected or self.quick_product.get()!=self.movement_product_display(selected):self.quick_selected_product_id=None;self.update_quick_current()
        self.schedule_ui_task("quick_product_search",lambda:self.show_quick_product_suggestions(force=True),70)

    def select_first_quick_product(self):
        results=self.quick_product_results(self.quick_product.get())
        if results:self.select_quick_product(int(results[0]["id"]));self.quick_register_button.focus_set()

    def select_quick_product(self, product_id):
        self.cancel_ui_task("quick_product_search")
        product=self.db.product(int(product_id))
        if not product:return
        self.quick_selected_product_id=int(product_id);self.quick_product.set(self.movement_product_display(product));self.hide_quick_product_suggestions();self.update_quick_current()

    def update_quick_current(self):
        if not hasattr(self, "quick_current"):return
        product=self.db.product(self.quick_selected_product_id) if self.quick_selected_product_id else None
        self.quick_current.configure(text=f"Saldo atual: {fmt_number(product['stock'])} {product['unit']}" if product else "Saldo atual: —")

    def on_quick_action_change(self, _value=None):
        if not hasattr(self, "quick_register_button"):return
        action=self.quick_action.get();definition=QUICK_STOCK_ACTIONS.get(action,QUICK_STOCK_ACTIONS["Defeito"]);delta=definition[2]
        color=COLORS["danger"] if delta<0 else COLORS["success"]
        signal="−1" if delta<0 else "+1"
        self.quick_register_button.configure(text=f"Registrar {action.lower()} ({signal} unidade)",fg_color=color,hover_color=color)
        self.save_interface_state()

    def register_quick_stock_movement(self):
        product_id=self.quick_selected_product_id
        if not product_id:
            results=self.quick_product_results(self.quick_product.get())
            if len(results)==1:product_id=int(results[0]["id"]);self.select_quick_product(product_id)
            else:messagebox.showwarning(APP_NAME,"Escolha um produto na lista de resultados.",parent=self);self.show_quick_product_suggestions(force=True);return
        responsible=self.quick_user.get().strip()
        if responsible not in self.user_names():messagebox.showwarning(APP_NAME,"Selecione um usuário cadastrado como responsável.",parent=self);return
        action=self.quick_action.get();product=self.db.product(product_id)
        try:self.db.add_quick_stock_movement(action,product_id,date.today().isoformat(),responsible)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        updated=self.db.product(product_id);signal="−1" if QUICK_STOCK_ACTIONS[action][2]<0 else "+1";result_color=COLORS["danger"] if action=="Defeito" else COLORS["success"]
        self.settings["quick_stock_action"]=action;self.settings["quick_stock_user"]=responsible;self.save_settings();self.refresh_all()
        self.quick_status.configure(text=f"{action} registrado: {product_label(product)}  •  {signal} unidade  •  novo saldo {fmt_number(updated['stock'])} {updated['unit']}",text_color=result_color)
        self.reset_quick_product_search()

    def reset_quick_product_search(self):
        self.quick_selected_product_id=None;self.quick_product.set("");self.hide_quick_product_suggestions();self.update_quick_current();self.after_idle(self.quick_product_entry.focus_set)

    def refresh_defect_return_page(self):
        if not hasattr(self, "quick_product_entry"):return
        self.refresh_user_controls()
        if self.quick_selected_product_id and not self.db.product(self.quick_selected_product_id):self.reset_quick_product_search()
        else:self.update_quick_current()

    def count_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Contagem","Faça o check-in físico do estoque e recupere a confiança dos saldos.").pack(fill="x",pady=(0,18))
        ctk.CTkLabel(page,text="A confiança diminui conforme passam os dias e aumentam a quantidade e a frequência das movimentações desde a última contagem.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w").pack(fill="x",pady=(0,12))
        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,16));self.count_cards=[]
        for title in ("A conferir","Conferidos hoje","Diferenças hoje","Confiança média"):
            card=Card(cards,height=92);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=16,pady=(13,2));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",20,"bold"));label.pack(anchor="w",padx=16);self.count_cards.append(label)
        body=ctk.CTkFrame(page,fg_color="transparent");body.pack(fill="both",expand=True);body.grid_columnconfigure(0,minsize=360);body.grid_columnconfigure(1,weight=1);body.grid_rowconfigure(0,weight=1)
        registered_users=self.user_names();saved_counter=self.settings.get("counter_name","");counter_values=registered_users or ["Cadastre um usuário na aba Cadastro"];selected_counter=saved_counter if saved_counter in registered_users else counter_values[0]
        form_card=Card(body,width=360);form_card.grid(row=0,column=0,sticky="nsew",padx=(0,16));form_card.grid_propagate(False)
        form=SmoothScrollableFrame(form_card,fg_color="transparent",corner_radius=11,scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"]);form.pack(fill="both",expand=True,padx=2,pady=2)
        ctk.CTkLabel(form,text="Novo check-in",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(anchor="w",padx=18,pady=(14,10));self.c_product=tk.StringVar();self.c_quantity=tk.StringVar();self.c_responsible=tk.StringVar(value=selected_counter);self.c_note=tk.StringVar();self.c_selected_product_id: int|None=None;self.count_product_suggestions_collapsed=not self.settings.get("count_products_expanded",False)
        def count_label(text):ctk.CTkLabel(form,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=20,pady=(0,4))
        count_label("Produto")
        self.c_product_search=ctk.CTkFrame(form,height=42,corner_radius=9,fg_color=COLORS["surface"],border_width=2,border_color=COLORS["accent"]);self.c_product_search.pack(fill="x",padx=20,pady=(0,8));self.c_product_search.grid_columnconfigure(1,weight=1);self.c_product_search.grid_propagate(False)
        ctk.CTkLabel(self.c_product_search,text="",image=self.icons["search"],width=36).grid(row=0,column=0,sticky="nsew",padx=(7,0),pady=4)
        self.c_product_entry=ctk.CTkEntry(self.c_product_search,textvariable=self.c_product,placeholder_text="Buscar produto, grupo ou variação...",height=34,corner_radius=0,border_width=0,fg_color="transparent");self.c_product_entry.grid(row=0,column=1,sticky="nsew",padx=(0,2),pady=3);self.c_product_entry.bind("<FocusIn>",lambda _event:self.schedule_ui_task("count_product_search",lambda:self.show_count_product_suggestions(force=True),80));self.c_product_entry.bind("<KeyRelease>",self.on_count_product_search);self.c_product_entry.bind("<Return>",lambda _event:self.select_first_count_product_suggestion())
        self.count_product_suggestions_toggle=ctk.CTkButton(self.c_product_search,text="",image=self.icons["expand" if self.count_product_suggestions_collapsed else "collapse"],width=36,height=32,corner_radius=7,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],command=self.toggle_count_product_suggestions);self.count_product_suggestions_toggle.grid(row=0,column=2,sticky="e",padx=(2,5),pady=4)
        self.count_product_suggestions=SmoothScrollableFrame(form,height=148,corner_radius=9,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.count_current=ctk.CTkLabel(form,text="Saldo do sistema: —",height=32,corner_radius=9,fg_color=COLORS["accent_soft"],text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold"));self.count_current.pack(fill="x",padx=20,pady=(0,8))
        count_label("Quantidade física contada")
        self.c_quantity_entry=ctk.CTkEntry(form,textvariable=self.c_quantity,height=36,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.c_quantity_entry.pack(fill="x",padx=20,pady=(0,8))
        count_label("Data (DD/MM/AA)")
        self.c_date_entry=MaskedDateEntry(form,COLORS,initial=date.today(),control_height=36);self.c_date_entry.pack(fill="x",padx=20,pady=(0,8))
        count_label("Responsável pela contagem")
        self.c_responsible_menu=ctk.CTkOptionMenu(form,variable=self.c_responsible,values=counter_values,height=36,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"]);self.c_responsible_menu.pack(fill="x",padx=20,pady=(0,8))
        count_label("Observação opcional")
        ctk.CTkEntry(form,textvariable=self.c_note,height=36,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]).pack(fill="x",padx=20,pady=(0,10))
        ctk.CTkButton(form,text="Confirmar contagem",height=40,corner_radius=10,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.register_count).pack(fill="x",padx=20,pady=(0,14))
        listing=Card(body);listing.grid(row=0,column=1,sticky="nsew");bar=ctk.CTkFrame(listing,fg_color="transparent");bar.pack(fill="x",padx=20,pady=16);ctk.CTkLabel(bar,text="Check-in dos produtos",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left")
        self.count_filter=tk.StringVar(value=self.settings.get("count_filter","todos"));ctk.CTkOptionMenu(bar,variable=self.count_filter,values=["todos","pendentes","verificados"],width=110,height=36,fg_color=COLORS["surface_alt"],button_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda _value:(self.refresh_counts(),self.save_interface_state())).pack(side="right")
        ctk.CTkButton(bar,text="Imprimir estoque",image=self.icons["print"],width=135,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.print_current_stock).pack(side="right",padx=(0,8))
        ctk.CTkButton(bar,text="Contar",image=self.icons["count"],width=95,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.prepare_count).pack(side="right",padx=(0,8))
        ctk.CTkButton(bar,text="Editar produto",image=self.icons["edit"],width=125,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_selected_count_product).pack(side="right",padx=(0,8))
        ctk.CTkButton(bar,text="Explicar",width=90,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.explain_confidence).pack(side="right",padx=(0,8))
        self.count_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto...",width=165,height=36,corner_radius=9);self.count_search.pack(side="right",padx=(0,8));self.count_search.insert(0,self.settings.get("count_search",""));self.count_search.bind("<KeyRelease>",lambda _event:self.schedule_ui_task("count_table_search",lambda:(self.refresh_counts(),self.save_interface_state()),100))
        self.count_tree=self.table(listing,("product","stock","checkin","date","responsible","confidence","difference"),("Produto","Estoque atual","Check-in","Última contagem","Responsável","Confiança","Diferença"),(170,75,75,120,80,85,85));self.count_tree.pack(fill="both",expand=True,padx=20,pady=(0,20));self.count_tree.bind("<<TreeviewSelect>>",self.on_count_tree_select);self.count_tree.bind("<Double-1>",lambda _e:self.prepare_count());self.count_confidence_cells=TreeConfidenceOverlay(self.count_tree,COLORS,activate=self.prepare_count);self.count_age_cells=TreeRelativeDateOverlay(self.count_tree,COLORS);self.configure_tables()
        if not self.count_product_suggestions_collapsed:self.schedule_ui_task("count_product_search",self.show_count_product_suggestions,80)
        return page

    def count_product_results(self,query=""):
        return self.db.products(query)

    def hide_count_product_suggestions(self):
        if hasattr(self,"count_product_suggestions"):
            self.count_product_suggestions.pack_forget();self.count_product_suggestions_collapsed=True;self.count_product_suggestions_toggle.configure(image=self.icons["expand"])

    def toggle_count_product_suggestions(self):
        self.count_product_suggestions_collapsed=not self.count_product_suggestions_collapsed
        if self.count_product_suggestions_collapsed:self.hide_count_product_suggestions()
        else:self.show_count_product_suggestions();self.c_product_entry.focus_set()
        self.save_interface_state()

    def show_count_product_suggestions(self,force=False):
        if not hasattr(self,"count_product_suggestions"):return
        if force:self.count_product_suggestions_collapsed=False
        self.count_product_suggestions_toggle.configure(image=self.icons["expand" if self.count_product_suggestions_collapsed else "collapse"])
        if self.count_product_suggestions_collapsed:
            self.count_product_suggestions.pack_forget();return
        for child in self.count_product_suggestions.winfo_children():child.destroy()
        results=self.count_product_results(self.c_product.get())
        if not results:
            ctk.CTkLabel(self.count_product_suggestions,text="Nenhum produto encontrado",height=36,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(fill="x",padx=10,pady=4)
        else:
            for product in results[:SEARCH_RESULT_LIMIT]:
                ctk.CTkButton(self.count_product_suggestions,text=self.movement_product_display(product),anchor="w",height=34,corner_radius=6,fg_color="transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_count_product(product_id,focus_quantity=True)).pack(fill="x",padx=5,pady=2)
            if len(results)>SEARCH_RESULT_LIMIT:ctk.CTkLabel(self.count_product_suggestions,text=f"Mostrando {SEARCH_RESULT_LIMIT} de {len(results)}. Continue digitando para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)
        self.count_product_suggestions.pack(fill="x",padx=20,pady=(0,8),before=self.count_current)

    def on_count_product_search(self,event=None):
        if event and event.keysym in ("Return","Escape"):
            if event.keysym=="Escape":self.hide_count_product_suggestions()
            return
        selected=self.db.product(self.c_selected_product_id) if self.c_selected_product_id else None
        if not selected or self.c_product.get()!=self.movement_product_display(selected):self.c_selected_product_id=None;self.update_count_current()
        self.schedule_ui_task("count_product_search",lambda:self.show_count_product_suggestions(force=True),100)

    def select_first_count_product_suggestion(self):
        results=self.count_product_results(self.c_product.get())
        if results:self.select_count_product(int(results[0]["id"]),focus_quantity=True)

    def select_count_product(self,product_id,focus_quantity=False):
        self.cancel_ui_task("count_product_search")
        product=self.db.product(int(product_id))
        if not product:return
        self.c_selected_product_id=int(product_id);self.c_product.set(self.movement_product_display(product));self.hide_count_product_suggestions();self.update_count_current()
        if focus_quantity:self.c_quantity_entry.focus_set()

    def update_count_current(self):
        pid=self.c_selected_product_id if hasattr(self,"c_selected_product_id") else None
        if not hasattr(self,"count_current"):return
        product=self.db.product(pid) if pid else None
        self.count_current.configure(text=f"Saldo do sistema: {fmt_number(product['stock'])} {product['unit']}" if product else "Saldo do sistema: —")

    def selected_count_product(self):
        selected=self.count_tree.selection() if hasattr(self,"count_tree") else ();return int(selected[0]) if selected else None

    def prepare_count(self):
        pid=self.selected_count_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para contar.",parent=self);return
        self.select_count_product(pid,focus_quantity=True)

    def on_count_tree_select(self,_event=None):
        pid=self.selected_count_product()
        if pid:self.select_count_product(pid)

    def edit_selected_count_product(self):
        pid=self.selected_count_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para editar.",parent=self);return
        product=self.db.product(pid)
        if not product:return
        dialog=ProductDialog(self,product);self.wait_window(dialog)
        if not dialog.result:return
        try:self.db.save_product(dialog.result,pid)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.select_count_product(pid);self.count_tree.selection_set(str(pid));self.count_tree.see(str(pid))

    def explain_confidence(self):
        pid=self.selected_count_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para consultar a confiança.",parent=self);return
        product=self.db.product(pid);trust=self.db.stock_confidence(pid,float(product["stock"]));last=datetime.strptime(trust["last_date"],"%Y-%m-%d").strftime("%d/%m/%y") if trust["last_date"] else "nunca realizada"
        messagebox.showinfo(APP_NAME,f"{product_label(product)}\n\nConfiança: {trust['score']}% — {trust['level']}\nÚltima contagem: {last}\nTempo considerado: {trust['days']} dia(s)\nMovimentações desde a contagem: {trust['movement_count']}\nQuantidade movimentada: {fmt_number(trust['moved_units'])} {product['unit']}\n\nQuanto mais tempo, operações diárias e unidades movimentadas, maior a necessidade de uma nova conferência.",parent=self)

    def register_count(self):
        pid=self.c_selected_product_id;responsible=self.c_responsible.get().strip()
        if not pid:messagebox.showwarning(APP_NAME,"Selecione um produto.",parent=self);return
        if responsible not in self.user_names():messagebox.showwarning(APP_NAME,"Selecione um usuário cadastrado como responsável pela contagem.",parent=self);return
        try:amount=float(self.c_quantity.get().replace(",","."));count_date=self.c_date_entry.get_date()
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        if amount<0:messagebox.showwarning(APP_NAME,"A quantidade contada não pode ser negativa.",parent=self);return
        previous=self.db.stock(pid);difference=amount-previous
        try:self.db.add_movement(pid,"inventario",amount,count_date.isoformat(),self.c_note.get().strip() or "Contagem física",checked_by=responsible)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.settings["counter_name"]=responsible;self.save_settings();self.c_quantity.set("");self.c_note.set("");self.c_date_entry.set_date(date.today());self.refresh_all();self.update_count_current();messagebox.showinfo(APP_NAME,f"Contagem confirmada.\nDiferença encontrada: {'+' if difference>0 else ''}{fmt_number(difference)}",parent=self);self.reset_count_product_search()

    def reset_count_product_search(self):
        self.c_selected_product_id=None;self.c_product.set("");self.hide_count_product_suggestions();self.update_count_current();self.after_idle(self.c_product_entry.focus_set)

    def refresh_counts(self):
        if not hasattr(self,"count_tree"):return
        selected_product=self.db.product(self.c_selected_product_id) if getattr(self,"c_selected_product_id",None) else None
        if not selected_product:self.c_selected_product_id=None;self.c_product.set("");self.update_count_current()
        search=self.count_search.get() if hasattr(self,"count_search") else "";items=self.db.products(search);self.count_tree.delete(*self.count_tree.get_children());all_items=self.db.products();pending=counted_today=differences_today=total_score=0;today=date.today().isoformat();infos=self.db.stock_confidences(all_items);visible_scores={};visible_ages={}
        for p in all_items:
            trust=infos[int(p["id"])];pending+=trust["checkin"]=="PENDENTE";counted_today+=trust["last_date"]==today;differences_today+=trust["last_date"]==today and trust["last_difference"] is not None and abs(trust["last_difference"])>.0000001;total_score+=trust["score"]
        selected_filter=self.count_filter.get() if hasattr(self,"count_filter") else "todos"
        for p in items:
            trust=infos[int(p["id"])];
            if selected_filter=="pendentes" and trust["checkin"]!="PENDENTE":continue
            if selected_filter=="verificados" and trust["checkin"]!="VERIFICADO":continue
            last=relative_past_date(trust["last_date"]);difference="—" if trust["last_difference"] is None else f"{'+' if trust['last_difference']>0 else ''}{fmt_number(trust['last_difference'])} {p['unit']}";visible_scores[int(p["id"])]=trust["score"];visible_ages[int(p["id"])]=(trust["days"] if trust["last_date"] else None,last);self.count_tree.insert("","end",iid=str(p["id"]),values=(product_label(p),f"{fmt_number(p['stock'])} {p['unit']}",trust["checkin"],"",trust["checked_by"]or"—","",difference))
        self.count_confidence_cells.set_scores(visible_scores)
        self.count_age_cells.set_ages(visible_ages)
        average=round(total_score/len(all_items)) if all_items else 0
        for label,text in zip(self.count_cards,(str(pending),str(counted_today),str(differences_today),f"{average}%"),strict=True):label.configure(text=text)
        if not self.count_product_suggestions_collapsed:self.show_count_product_suggestions()

    def kit_conversion_page(self):
        page = SmoothScrollableFrame(
            self.content, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COLORS["accent"],
            scrollbar_button_hover_color=COLORS["accent_hover"],
        )
        PageTitle(
            page,
            "Montagem / Desmontagem",
            "Transforme kits da mesma cor e registre automaticamente a saída da origem e a entrada do destino.",
        ).pack(fill="x", pady=(0, 20))

        saved_mode = self.settings.get("kit_conversion_mode", "Montagem")
        self.kit_mode = tk.StringVar(value="Desmontagem" if saved_mode in ("Desmontagem", "Desmembramento") else "Montagem")
        self.kit_product_search = tk.StringVar()
        self.kit_quantity = tk.StringVar(value="1")
        self.kit_user = tk.StringVar(value=self.settings.get("kit_conversion_user", ""))
        self.kit_note = tk.StringVar()
        self.kit_primary_product_id: int | None = None
        self.kit_secondary_mapping: dict[str, int] = {}
        self.kit_editing_batch_id: int | None = None

        mode_card = Card(page); mode_card.pack(fill="x", pady=(0, 16))
        mode_row = ctk.CTkFrame(mode_card, fg_color="transparent"); mode_row.pack(fill="x", padx=22, pady=18)
        mode_text = ctk.CTkFrame(mode_row, fg_color="transparent"); mode_text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(mode_text, text="O que você deseja fazer?", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(mode_text, text="A direção da conversão define automaticamente qual kit sai e qual kit entra.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(anchor="w", pady=(4, 0))
        self.kit_mode_selector = ctk.CTkSegmentedButton(
            mode_row, variable=self.kit_mode, values=["Montagem", "Desmontagem"],
            width=330, height=42, corner_radius=9, selected_color=COLORS["accent"],
            selected_hover_color=COLORS["accent_hover"], command=self.on_kit_mode_change,
        )
        self.kit_mode_selector.pack(side="right", padx=(18, 0)); self.kit_mode_selector.set(self.kit_mode.get())

        composer = Card(page); composer.pack(fill="x", pady=(0, 16)); composer.grid_columnconfigure((0, 1), weight=1)
        primary = ctk.CTkFrame(composer, fg_color="transparent"); primary.grid(row=0, column=0, sticky="nsew", padx=(22, 14), pady=20)
        secondary = ctk.CTkFrame(composer, fg_color="transparent"); secondary.grid(row=0, column=1, sticky="nsew", padx=(14, 22), pady=20)

        self.kit_primary_title = ctk.CTkLabel(primary, text="", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 15, "bold"))
        self.kit_primary_title.pack(anchor="w")
        self.kit_primary_help = ctk.CTkLabel(primary, text="", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10))
        self.kit_primary_help.pack(anchor="w", pady=(4, 10))
        search_box = ctk.CTkFrame(primary, height=42, corner_radius=9, fg_color=COLORS["surface"], border_width=2, border_color=COLORS["accent"])
        search_box.pack(fill="x"); search_box.grid_columnconfigure(1, weight=1); search_box.grid_propagate(False)
        ctk.CTkLabel(search_box, text="", image=self.icons["search"], width=42).grid(row=0, column=0, sticky="nsew", padx=(8, 2), pady=4)
        self.kit_product_entry = ctk.CTkEntry(
            search_box, textvariable=self.kit_product_search,
            placeholder_text="Buscar cor, variação ou grupo...", height=34,
            corner_radius=0, border_width=0, fg_color="transparent",
        )
        self.kit_product_entry.grid(row=0, column=1, sticky="nsew", padx=(0, 6), pady=3)
        self.kit_product_entry.bind("<KeyRelease>", lambda _event:self.schedule_ui_task("kit_product_search",self.show_kit_product_suggestions,100))
        self.kit_product_entry.bind("<FocusIn>", lambda _event:self.schedule_ui_task("kit_product_search",self.show_kit_product_suggestions,80))
        self.kit_product_entry.bind("<Return>", lambda _event:self.select_first_kit_product())
        self.kit_primary_suggestions = SmoothScrollableFrame(
            primary, height=180, corner_radius=9, fg_color=COLORS["surface_alt"],
            border_width=1, border_color=COLORS["border"],
            scrollbar_button_color=COLORS["accent"], scrollbar_button_hover_color=COLORS["accent_hover"],
        )
        self.kit_primary_suggestions.pack(fill="x", pady=(10, 0))

        self.kit_secondary_title = ctk.CTkLabel(secondary, text="", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 15, "bold"))
        self.kit_secondary_title.pack(anchor="w")
        self.kit_secondary_help = ctk.CTkLabel(secondary, text="", wraplength=520, justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10))
        self.kit_secondary_help.pack(anchor="w", pady=(4, 10))
        self.kit_secondary = tk.StringVar(value="Selecione primeiro o kit acima")
        self.kit_secondary_menu = ctk.CTkOptionMenu(
            secondary, variable=self.kit_secondary, values=[self.kit_secondary.get()], height=42,
            corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"],
            button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"],
            dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"],
            command=lambda _value:self.update_kit_preview(),
        )
        self.kit_secondary_menu.pack(fill="x")
        self.kit_match_status = ctk.CTkLabel(
            secondary, text="Escolha um kit para ver somente as opções da mesma cor.",
            wraplength=520, justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10),
        )
        self.kit_match_status.pack(anchor="w", pady=(12, 0))

        details = ctk.CTkFrame(composer, fg_color="transparent"); details.grid(row=1, column=0, columnspan=2, sticky="ew", padx=22, pady=(0, 20))
        details.grid_columnconfigure((0, 1, 2), weight=1)
        for column, text_value in enumerate(("Quantidade de kits", "Data (DD/MM/AA)", "Usuário responsável")):
            ctk.CTkLabel(details, text=text_value, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).grid(row=0, column=column, sticky="w", padx=(0 if column == 0 else 8, 8 if column < 2 else 0), pady=(0, 6))
        self.kit_quantity_entry = ctk.CTkEntry(details, textvariable=self.kit_quantity, height=42, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.kit_quantity_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8)); self.kit_quantity.trace_add("write", lambda *_args:self.update_kit_preview())
        self.kit_date_entry = MaskedDateEntry(details, COLORS, initial=date.today()); self.kit_date_entry.grid(row=1, column=1, sticky="ew", padx=8)
        self.kit_user_menu = ctk.CTkOptionMenu(details, variable=self.kit_user, values=["Cadastre um usuário"], height=42, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"], command=lambda _value:self.save_interface_state())
        self.kit_user_menu.grid(row=1, column=2, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(details, text="Observação opcional", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 6))
        ctk.CTkEntry(details, textvariable=self.kit_note, height=42, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"], placeholder_text="Ex.: montagem para completar um pedido").grid(row=3, column=0, columnspan=3, sticky="ew")

        preview = Card(page); preview.pack(fill="x", pady=(0, 16))
        preview_header = ctk.CTkFrame(preview, fg_color="transparent"); preview_header.pack(fill="x", padx=22, pady=(18, 12))
        self.kit_preview_title = ctk.CTkLabel(preview_header, text="Prévia da conversão", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")); self.kit_preview_title.pack(side="left")
        self.kit_preview_badge = ctk.CTkLabel(preview_header, text="AGUARDANDO SELEÇÃO", fg_color=COLORS["surface_alt"], corner_radius=8, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 9, "bold"), padx=12, pady=6); self.kit_preview_badge.pack(side="right")
        preview_grid = ctk.CTkFrame(preview, fg_color="transparent"); preview_grid.pack(fill="x", padx=22, pady=(0, 18)); preview_grid.grid_columnconfigure((0, 2), weight=1)
        self.kit_source_card = ctk.CTkFrame(preview_grid, fg_color=COLORS["surface_alt"], corner_radius=10, border_width=1, border_color=COLORS["border"]); self.kit_source_card.grid(row=0, column=0, sticky="nsew")
        self.kit_source_name = ctk.CTkLabel(self.kit_source_card, text="Kit de origem", wraplength=460, justify="center", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 13, "bold")); self.kit_source_name.pack(padx=16, pady=(16, 5))
        self.kit_source_change = ctk.CTkLabel(self.kit_source_card, text="−", text_color=COLORS["danger"], font=ctk.CTkFont("Inter", 20, "bold")); self.kit_source_change.pack()
        self.kit_source_balance = ctk.CTkLabel(self.kit_source_card, text="Saldo atual → saldo projetado", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)); self.kit_source_balance.pack(padx=16, pady=(4, 16))
        ctk.CTkLabel(preview_grid, text="→", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 27, "bold"), width=70).grid(row=0, column=1, padx=10)
        self.kit_target_card = ctk.CTkFrame(preview_grid, fg_color=COLORS["surface_alt"], corner_radius=10, border_width=1, border_color=COLORS["border"]); self.kit_target_card.grid(row=0, column=2, sticky="nsew")
        self.kit_target_name = ctk.CTkLabel(self.kit_target_card, text="Kit de destino", wraplength=460, justify="center", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 13, "bold")); self.kit_target_name.pack(padx=16, pady=(16, 5))
        self.kit_target_change = ctk.CTkLabel(self.kit_target_card, text="+", text_color=COLORS["success"], font=ctk.CTkFont("Inter", 20, "bold")); self.kit_target_change.pack()
        self.kit_target_balance = ctk.CTkLabel(self.kit_target_card, text="Saldo atual → saldo projetado", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)); self.kit_target_balance.pack(padx=16, pady=(4, 16))

        actions = ctk.CTkFrame(page, fg_color="transparent"); actions.pack(fill="x", pady=(0, 20))
        self.kit_cancel_edit_button = ctk.CTkButton(actions, text="Cancelar edição", width=140, height=42, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.cancel_kit_conversion_edit)
        self.kit_save_button = ctk.CTkButton(actions, text="Confirmar montagem", image=self.icons["kit_conversion"], width=210, height=44, corner_radius=10, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], state="disabled", command=self.register_kit_conversion)
        self.kit_save_button.pack(side="right")
        ctk.CTkLabel(actions, text="A confirmação cria uma única movimentação com duas alterações vinculadas.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(side="left")

        self.on_kit_mode_change(self.kit_mode.get(), refresh_suggestions=False)
        return page

    def kit_mode_key(self) -> str:
        return "desmembramento" if self.kit_mode.get() == "Desmontagem" else "montagem"

    def kit_primary_products(self, query: str = "") -> list[sqlite3.Row]:
        return [
            product for product in self.db.products(query)
            if kit_piece_count(product) in (4, 5)
        ]

    def show_kit_product_suggestions(self):
        if not hasattr(self, "kit_primary_suggestions"):
            return
        for child in self.kit_primary_suggestions.winfo_children(): child.destroy()
        products = self.kit_primary_products(self.kit_product_search.get())
        if not products:
            ctk.CTkLabel(self.kit_primary_suggestions, text="Nenhum kit de 4 ou 5 peças encontrado", height=38, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(fill="x", padx=10, pady=5)
            return
        for product in products[:SEARCH_RESULT_LIMIT]:
            selected = int(product["id"]) == self.kit_primary_product_id
            label = f"{product_label(product)}    •    Saldo: {fmt_number(product['stock'])} {product['unit']}"
            ctk.CTkButton(
                self.kit_primary_suggestions, text=label, anchor="w", height=38,
                corner_radius=7, fg_color=COLORS["nav_selected"] if selected else "transparent",
                hover_color=COLORS["surface_hover"], text_color=COLORS["accent"] if selected else COLORS["text"],
                font=ctk.CTkFont("Inter", 10, "bold" if selected else "normal"),
                command=lambda product_id=int(product["id"]):self.select_kit_primary_product(product_id),
            ).pack(fill="x", padx=5, pady=2)
        if len(products)>SEARCH_RESULT_LIMIT:
            ctk.CTkLabel(self.kit_primary_suggestions,text=f"Mostrando {SEARCH_RESULT_LIMIT} de {len(products)}. Continue digitando para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)

    def select_first_kit_product(self):
        products = self.kit_primary_products(self.kit_product_search.get())
        if products:self.select_kit_primary_product(int(products[0]["id"]))

    def select_kit_primary_product(self, product_id: int):
        self.cancel_ui_task("kit_product_search")
        product = self.db.product(int(product_id))
        if not product or kit_piece_count(product) not in (4, 5):
            return
        self.kit_primary_product_id = int(product_id)
        self.kit_product_search.set(product_label(product))
        self.refresh_kit_secondary_options()
        self.show_kit_product_suggestions()
        self.update_kit_preview()

    def refresh_kit_secondary_options(self, selected_product_id: int | None = None):
        primary = self.db.product(self.kit_primary_product_id) if self.kit_primary_product_id else None
        matches = compatible_smaller_kits(primary, self.db.products()) if primary else []
        self.kit_secondary_mapping = {
            f"{product_label(product)}  •  Saldo: {fmt_number(product['stock'])} {product['unit']}": int(product["id"])
            for product in matches
        }
        values = list(self.kit_secondary_mapping)
        selected_label = next((label for label, product_id in self.kit_secondary_mapping.items() if product_id == selected_product_id), None)
        if not values:
            placeholder = "Nenhum kit menor da mesma cor cadastrado" if primary else "Selecione primeiro o kit de 4 ou 5 peças"
            self.kit_secondary_mapping = {}
            values = [placeholder]; selected_label = placeholder
        self.kit_secondary_menu.configure(values=values)
        self.kit_secondary.set(selected_label or values[0])
        if primary and matches:
            sizes = " ou ".join(str(kit_piece_count(product)) for product in matches)
            self.kit_match_status.configure(text=f"Correspondência exata: mesma família e mesma cor/variação. Disponível em {sizes} peças.", text_color=COLORS["success"])
        elif primary:
            self.kit_match_status.configure(text="Não existe um kit menor cadastrado com exatamente a mesma cor/variação.", text_color=COLORS["danger"])
        else:
            self.kit_match_status.configure(text="Escolha um kit para ver somente as opções da mesma cor.", text_color=COLORS["muted"])

    def selected_kit_conversion_products(self) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        primary = self.db.product(self.kit_primary_product_id) if self.kit_primary_product_id else None
        secondary_id = self.kit_secondary_mapping.get(self.kit_secondary.get())
        secondary = self.db.product(secondary_id) if secondary_id else None
        if self.kit_mode_key() == "montagem":
            return secondary, primary
        return primary, secondary

    def on_kit_mode_change(self, _value=None, refresh_suggestions=True):
        if not hasattr(self, "kit_primary_title"):
            return
        montage = self.kit_mode_key() == "montagem"
        self.kit_primary_title.configure(text="1. Qual kit você deseja montar?" if montage else "1. Qual kit será desmontado?")
        self.kit_primary_help.configure(text="Escolha um kit final de 4 ou 5 peças." if montage else "Escolha o kit de origem de 4 ou 5 peças.")
        self.kit_secondary_title.configure(text="2. Qual kit foi usado como base?" if montage else "2. Em qual kit menor ele foi transformado?")
        self.kit_secondary_help.configure(text="A lista mostra somente kits menores da mesma cor/variação." if montage else "Escolha o resultado de 2 ou 4 peças da mesma cor/variação.")
        self.kit_save_button.configure(text="Atualizar montagem" if montage and self.kit_editing_batch_id else "Confirmar montagem" if montage else "Atualizar desmontagem" if self.kit_editing_batch_id else "Confirmar desmontagem")
        self.refresh_kit_secondary_options()
        if refresh_suggestions:self.show_kit_product_suggestions()
        self.update_kit_preview()
        self.save_interface_state()

    def update_kit_preview(self):
        if not hasattr(self, "kit_preview_badge"):
            return
        source, target = self.selected_kit_conversion_products()
        try: amount = float(self.kit_quantity.get().replace(",", "."))
        except ValueError: amount = 0
        valid_amount = amount > 0 and math.isfinite(amount) and amount.is_integer()
        if not source or not target or not valid_amount:
            self.kit_preview_badge.configure(text="AGUARDANDO SELEÇÃO", fg_color=COLORS["surface_alt"], text_color=COLORS["muted"])
            self.kit_source_name.configure(text="Kit de origem"); self.kit_source_change.configure(text="−")
            self.kit_source_balance.configure(text="Saldo atual → saldo projetado")
            self.kit_target_name.configure(text="Kit de destino"); self.kit_target_change.configure(text="+")
            self.kit_target_balance.configure(text="Saldo atual → saldo projetado")
            self.kit_save_button.configure(state="disabled")
            return
        source_stock = float(source["stock"]); target_stock = float(target["stock"])
        source_projected = source_stock - amount; target_projected = target_stock + amount
        badge = "MONTAGEM" if self.kit_mode_key() == "montagem" else "DESMEMBRAMENTO"
        self.kit_preview_badge.configure(text=badge, fg_color=COLORS["accent_soft"], text_color=COLORS["accent"])
        self.kit_source_name.configure(text=product_label(source)); self.kit_source_change.configure(text=f"−{fmt_number(amount)} {source['unit']}")
        self.kit_source_balance.configure(text=f"Saldo: {fmt_number(source_stock)} → {fmt_number(source_projected)} {source['unit']}", text_color=COLORS["danger"] if source_projected < 0 else COLORS["muted"])
        self.kit_target_name.configure(text=product_label(target)); self.kit_target_change.configure(text=f"+{fmt_number(amount)} {target['unit']}")
        self.kit_target_balance.configure(text=f"Saldo: {fmt_number(target_stock)} → {fmt_number(target_projected)} {target['unit']}")
        self.kit_save_button.configure(state="normal")

    def register_kit_conversion(self):
        source, target = self.selected_kit_conversion_products()
        if not source or not target:
            messagebox.showwarning(APP_NAME, "Selecione os kits de origem e destino.", parent=self); return
        if self.kit_user.get() not in self.user_names():
            messagebox.showwarning(APP_NAME, "Cadastre e selecione um usuário responsável na aba Cadastro.", parent=self); return
        try:
            amount = float(self.kit_quantity.get().replace(",", "."))
            movement_date = self.kit_date_entry.get_date().isoformat()
            editing_batch_id = self.kit_editing_batch_id
            arguments = (
                self.kit_mode_key(), int(source["id"]), int(target["id"]), amount,
                movement_date, self.kit_note.get().strip(), self.kit_user.get(),
            )
            if editing_batch_id:self.db.update_kit_conversion(editing_batch_id, *arguments)
            else:self.db.add_kit_conversion(*arguments)
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error) or "Revise os dados da conversão.", parent=self); return
        description = f"{product_label(source)} → {product_label(target)}"
        action = "atualizada" if editing_batch_id else "registrada"
        self.reset_kit_conversion_form()
        self.refresh_all()
        self.show_movement_result(f"Conversão {action} com sucesso.\n\n{description}\nQuantidade: {fmt_number(amount)} kit(s).")

    def reset_kit_conversion_form(self):
        self.kit_editing_batch_id = None; self.kit_primary_product_id = None
        self.kit_product_search.set(""); self.kit_quantity.set("1"); self.kit_note.set("")
        self.kit_date_entry.set_date(date.today()); self.kit_cancel_edit_button.pack_forget()
        self.refresh_kit_secondary_options(); self.on_kit_mode_change(self.kit_mode.get())

    def cancel_kit_conversion_edit(self):
        self.reset_kit_conversion_form()

    def load_kit_conversion_for_edit(self, batch_id: int):
        conversion = self.db.kit_conversion_batch(batch_id)
        if not conversion:
            messagebox.showwarning(APP_NAME, "Essa montagem ou desmontagem não existe mais.", parent=self); return
        self.show_page("kit_conversion")
        conversion = self.db.kit_conversion_batch(batch_id)
        self.kit_editing_batch_id = batch_id
        self.kit_mode.set("Montagem" if conversion["mode"] == "montagem" else "Desmontagem")
        primary = conversion["target"] if conversion["mode"] == "montagem" else conversion["source"]
        secondary = conversion["source"] if conversion["mode"] == "montagem" else conversion["target"]
        self.kit_primary_product_id = int(primary["product_id"])
        product = self.db.product(self.kit_primary_product_id)
        self.kit_product_search.set(product_label(product) if product else "")
        self.refresh_kit_secondary_options(int(secondary["product_id"]))
        self.kit_quantity.set(fmt_number(conversion["quantity"]))
        self.kit_date_entry.set_date(datetime.strptime(conversion["batch"]["movement_date"], "%Y-%m-%d").date())
        users = self.user_names(); responsible = str(conversion["batch"]["performed_by"] or "")
        self.kit_user.set(responsible if responsible in users else (users[0] if users else ""))
        stored_reason = str(conversion["batch"]["reason"] or "")
        self.kit_note.set(stored_reason.partition(" — ")[2])
        self.kit_cancel_edit_button.pack(side="right", padx=(0, 10))
        self.on_kit_mode_change(self.kit_mode.get()); self.update_kit_preview()

    def refresh_kit_conversion(self):
        if not hasattr(self, "kit_primary_suggestions"):
            return
        users = self.user_names(); values = users or ["Cadastre um usuário na aba Cadastro"]
        self.kit_user_menu.configure(values=values)
        if self.kit_user.get() not in users:self.kit_user.set(users[0] if users else values[0])
        if self.kit_primary_product_id and not self.db.product(self.kit_primary_product_id):
            self.kit_primary_product_id = None; self.kit_product_search.set("")
        selected_secondary = self.kit_secondary_mapping.get(self.kit_secondary.get())
        self.refresh_kit_secondary_options(selected_secondary)
        self.show_kit_product_suggestions(); self.update_kit_preview()

    def movements_page(self):
        page = SmoothScrollableFrame(self.content, fg_color="transparent", corner_radius=0, scrollbar_button_color=COLORS["accent"], scrollbar_button_hover_color=COLORS["accent_hover"])
        self.movement_page = page
        PageTitle(page, "Movimentações", "Monte um conjunto de produtos, revise e registre tudo de uma vez.").pack(fill="x", pady=(0, 18))
        self.movement_draft: list[dict] = []
        self.draft_edit_index: int | None = None
        self.editing_batch_id: int | None = None
        self.m_selected_product_id: int | None = None
        self.m_operation = tk.StringVar(value=self.settings.get("movement_operation","Entrada"))
        self.m_product = tk.StringVar()
        self.m_quantity = tk.StringVar()
        self.m_reason = tk.StringVar()
        self.m_user = tk.StringVar(value=self.settings.get("movement_user",""))
        self.product_suggestions_collapsed = not self.settings.get("movement_products_expanded",False)

        section_bar = ctk.CTkFrame(page, fg_color=COLORS["surface_alt"], corner_radius=11, border_width=1, border_color=COLORS["border"])
        section_bar.pack(anchor="w", pady=(0, 18))
        self.movement_new_tab = ctk.CTkButton(section_bar, text="Nova movimentação", image=self.icons["plus"], width=190, height=42, corner_radius=9, command=lambda:self.show_movement_section("new"))
        self.movement_new_tab.pack(side="left", padx=(4, 2), pady=4)
        self.movement_history_tab = ctk.CTkButton(section_bar, text="Histórico", image=self.icons["movements"], width=150, height=42, corner_radius=9, command=lambda:self.show_movement_section("history"))
        self.movement_history_tab.pack(side="left", padx=(2, 4), pady=4)

        self.movement_new_panel = ctk.CTkFrame(page, fg_color="transparent")
        self.movement_history_panel = ctk.CTkFrame(page, fg_color="transparent")

        sales_import = Card(self.movement_new_panel); sales_import.pack(fill="x",pady=(0,16))
        sales_text=ctk.CTkFrame(sales_import,fg_color="transparent");sales_text.pack(fill="x",expand=True,padx=20,pady=(16,8))
        ctk.CTkLabel(sales_text,text="Baixa automática com lista",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w")
        ctk.CTkLabel(sales_text,text="Leia SKU e quantidade, confira os vínculos e leve a baixa para o conjunto abaixo.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",pady=(4,0))
        sales_actions=ctk.CTkFrame(sales_import,fg_color="transparent");sales_actions.pack(fill="x",padx=20,pady=(0,16))
        ctk.CTkButton(sales_actions,text="Importar Lista Shopee",image=self.icons["upload"],height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=lambda:self.import_sales_list("shopee")).pack(side="left",padx=(0,8))
        ctk.CTkButton(sales_actions,text="Importar Lista Mercado Livre",image=self.icons["upload"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda:self.import_sales_list("mercado_livre")).pack(side="left")

        composer = Card(self.movement_new_panel)
        composer.pack(fill="x", pady=(0, 16))
        composer.grid_columnconfigure(0, weight=2); composer.grid_columnconfigure(1, weight=3)

        details = ctk.CTkFrame(composer, fg_color="transparent")
        details.grid(row=0, column=0, sticky="nsew", padx=(20, 14), pady=18)
        ctk.CTkLabel(details, text="Dados da movimentação", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 15, "bold")).pack(anchor="w", pady=(0, 14))

        def detail_label(text):
            ctk.CTkLabel(details, text=text, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10, "bold")).pack(anchor="w", pady=(0, 6))

        detail_label("Operação para todo o conjunto")
        self.m_operation_menu = ctk.CTkOptionMenu(details, variable=self.m_operation, values=[""], height=38, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"], command=lambda _value: self.on_operation_change())
        self.m_operation_menu.pack(fill="x",pady=(0,12))
        detail_label("Data e usuário responsável")
        date_user = ctk.CTkFrame(details, fg_color="transparent")
        date_user.pack(fill="x", pady=(0, 12)); date_user.grid_columnconfigure((0, 1), weight=1)
        self.m_date_entry = MaskedDateEntry(date_user, COLORS, initial=date.today())
        self.m_date_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.m_user_menu=ctk.CTkOptionMenu(date_user,variable=self.m_user,values=["Cadastre um usuário"],height=38,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"],command=lambda _value:self.save_interface_state())
        self.m_user_menu.grid(row=0,column=1,sticky="ew",padx=(6,0))
        detail_label("Motivo ou observação do conjunto")
        ctk.CTkEntry(details, textvariable=self.m_reason, height=38, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"]).pack(fill="x")

        items = ctk.CTkFrame(composer, fg_color="transparent")
        items.grid(row=0, column=1, sticky="nsew", padx=(14, 20), pady=18)
        item_header = ctk.CTkFrame(items, fg_color="transparent")
        item_header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(item_header, text="Produtos do conjunto", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 15, "bold")).pack(side="left")
        self.current_stock = ctk.CTkLabel(item_header, text="Saldo atual: —", text_color=COLORS["accent"], font=ctk.CTkFont("Inter", 10, "bold"))
        self.current_stock.pack(side="right")
        self.m_draft_total_label = ctk.CTkLabel(item_header, text="Quantidade total: 0 itens", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 10, "bold"))
        self.m_draft_total_label.pack(side="right", padx=(0, 18))
        add_row = ctk.CTkFrame(items, fg_color="transparent")
        add_row.pack(fill="x", pady=(0, 10)); add_row.grid_columnconfigure(0, weight=1)
        self.m_product_search=ctk.CTkFrame(add_row,height=42,corner_radius=9,fg_color=COLORS["surface"],border_width=2,border_color=COLORS["accent"])
        self.m_product_search.grid(row=0,column=0,sticky="ew",padx=(0,8));self.m_product_search.grid_columnconfigure(1,weight=1);self.m_product_search.grid_propagate(False)
        ctk.CTkLabel(self.m_product_search,text="",image=self.icons["search"],width=42).grid(row=0,column=0,sticky="nsew",padx=(9,2),pady=4)
        self.m_product_entry=ctk.CTkEntry(self.m_product_search,textvariable=self.m_product,placeholder_text="Buscar produto, grupo ou variação...",height=34,corner_radius=0,border_width=0,fg_color="transparent")
        self.m_product_entry.grid(row=0,column=1,sticky="nsew",padx=(0,2),pady=3);self.m_product_entry.bind("<FocusIn>",lambda _event:self.schedule_ui_task("movement_product_search",lambda:self.show_product_suggestions(force=True),80));self.m_product_entry.bind("<KeyRelease>",self.on_product_search);self.m_product_entry.bind("<Return>",lambda _event:self.select_first_product_suggestion())
        self.product_suggestions_toggle = ctk.CTkButton(self.m_product_search, text="", image=self.icons["expand" if self.product_suggestions_collapsed else "collapse"], width=36, height=32, corner_radius=7, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], command=self.toggle_product_suggestions)
        self.product_suggestions_toggle.grid(row=0,column=2,sticky="e",padx=(2,5),pady=4)
        self.m_quantity_entry = ctk.CTkEntry(add_row, textvariable=self.m_quantity, placeholder_text="Quantidade", width=120, height=38, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.m_quantity_entry.grid(row=0, column=1, padx=(0, 8))
        self.m_add_button = ctk.CTkButton(add_row, text="Adicionar", width=105, height=38, corner_radius=9, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.add_draft_item)
        self.m_add_button.grid(row=0, column=2)
        self.product_suggestions=SmoothScrollableFrame(items,height=160,corner_radius=9,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.draft_tree = self.table(items, ("product", "quantity"), ("Produto", "Quantidade"), (360, 120))
        self.draft_tree.configure(height=4)
        self.draft_tree.pack(fill="x")
        draft_actions = ctk.CTkFrame(items, fg_color="transparent")
        draft_actions.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(draft_actions, text="Remover", image=self.icons["trash"], width=95, height=34, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["danger"], command=self.remove_draft_item).pack(side="left")
        ctk.CTkButton(draft_actions, text="Editar item", image=self.icons["edit"], width=105, height=34, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.edit_draft_item).pack(side="left", padx=(6, 0))
        self.m_cancel_edit_button = ctk.CTkButton(draft_actions, text="Cancelar edição", width=120, height=34, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], command=self.cancel_batch_edit)
        self.m_save_button = ctk.CTkButton(draft_actions, text="Salvar movimentação (0 produtos)", height=40, corner_radius=9, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], state="disabled", command=self.register_movement)
        self.m_save_button.pack(side="right")

        history = Card(self.movement_history_panel)
        history.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(history, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=16)
        history_text = ctk.CTkFrame(bar, fg_color="transparent"); history_text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(history_text, text="Histórico de movimentações", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(history_text, text="Cada linha representa um conjunto fechado. Abra para ver, editar ou excluir os produtos.", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(anchor="w", pady=(3, 0))
        self.history_filter = tk.StringVar(value=self.settings.get("history_filter","Todas as operações"))
        self.history_filter_menu = ctk.CTkOptionMenu(bar, variable=self.history_filter, values=["Todas as operações"], width=175, height=36, fg_color=COLORS["surface_alt"], button_color=COLORS["surface_hover"], text_color=COLORS["text"], command=lambda _value:(self.refresh_movements(),self.save_interface_state()))
        self.history_filter_menu.pack(side="right")
        ctk.CTkButton(bar, text="Abrir detalhes", image=self.icons["expand"], width=135, height=36, corner_radius=9, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.open_history_details).pack(side="right", padx=(8, 8))
        self.history_tree = self.table(history, ("date", "operation", "items", "products", "user", "reason"), ("Data", "Operação", "Itens", "Produtos do conjunto", "Usuário", "Observação"), (85, 130, 70, 390, 135, 220))
        self.history_tree.column("products", anchor="w")
        self.history_tree.column("reason", anchor="w")
        self.history_tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.history_tree.bind("<Double-1>", lambda _event: self.open_history_details())
        self.configure_tables()
        if not self.product_suggestions_collapsed:self.schedule_ui_task("movement_product_search",self.show_product_suggestions,80)
        self.show_movement_section(self.settings.get("movement_section", "new"), refresh_history=False)
        return page

    def show_movement_section(self, section: str, refresh_history: bool = True):
        if not hasattr(self, "movement_new_panel"):
            return
        section = "history" if section == "history" else "new"
        self.movement_new_panel.pack_forget(); self.movement_history_panel.pack_forget()
        selected_button = self.movement_history_tab if section == "history" else self.movement_new_tab
        other_button = self.movement_new_tab if section == "history" else self.movement_history_tab
        selected_button.configure(fg_color=COLORS["nav_selected"], hover_color=COLORS["accent_soft"], text_color=COLORS["accent"], border_width=1, border_color=COLORS["accent"])
        other_button.configure(fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], border_width=0)
        panel = self.movement_history_panel if section == "history" else self.movement_new_panel
        panel.pack(fill="both", expand=True)
        self.settings["movement_section"] = section
        self.save_interface_state()
        if section == "history" and refresh_history:
            self.refresh_movements()
        try:self.movement_page.after_idle(lambda:self.movement_page._parent_canvas.yview_moveto(0))
        except (AttributeError, tk.TclError):pass

    def import_sales_list(self, source: str):
        labels={"shopee":"Shopee","mercado_livre":"Mercado Livre"};source_label=labels[source]
        if not self.db.products():messagebox.showwarning(APP_NAME,"Cadastre pelo menos um produto antes de importar uma lista.",parent=self);return
        selected=filedialog.askopenfilename(parent=self,title=f"Selecionar Lista {source_label}",filetypes=[("Arquivo PDF","*.pdf")])
        if not selected:return
        try:items=read_sales_list(Path(selected),source)
        except SalesListError as error:messagebox.showerror(APP_NAME,str(error),parent=self);return

        for index,item in enumerate(items,1):
            mapping=self.db.sku_mapping_for(item.sku);products=self.db.sku_mapping_products(int(mapping["id"])) if mapping else []
            if products:continue
            dialog=SkuMappingEditorDialog(self,sku=item.sku,locked_sku=True,context=f"Novo SKU encontrado ({index} de {len(items)}). Marque um ou mais produtos para memorizar este vínculo.")
            self.wait_window(dialog)
            if not dialog.result:
                messagebox.showinfo(APP_NAME,"Importação cancelada. Os vínculos que você já salvou foram mantidos.",parent=self);return

        try:review=SalesListReviewDialog(self,source_label,Path(selected).name,items);self.wait_window(review)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        if not review.result:return
        if self.movement_draft and not messagebox.askyesno(APP_NAME,"Substituir os produtos que já estão no conjunto pela baixa desta lista?",icon="warning",parent=self):return

        self.refresh_operation_controls();outgoing=self.db.operation("saida")
        if not outgoing or not outgoing["active"]:
            outgoing=next((operation for operation in self.db.operations() if operation["effect"]=="negative"),None)
        if not outgoing:messagebox.showwarning(APP_NAME,"Cadastre ou reative uma operação de saída antes de concluir a importação.",parent=self);return
        self.m_operation.set(str(outgoing["name"]));self.on_operation_change()
        self.movement_draft=[{"product_id":int(row["product_id"]),"quantity":float(row["quantity"])} for row in review.product_rows]
        self.draft_edit_index=None;self.m_selected_product_id=None;self.m_product.set("");self.m_quantity.set("");self.m_add_button.configure(text="Adicionar")
        self.m_reason.set(f"Lista {source_label} - {Path(selected).name}");self.refresh_draft();self.update_current_stock()
        messagebox.showinfo(APP_NAME,"Lista conferida e adicionada à movimentação.\n\nEscolha a data e o usuário responsável, revise o conjunto e clique em Salvar movimentação para efetuar a baixa.",parent=self)

    def product_map(self):return {f"{product_label(p)}  [{p['unit']}]":int(p["id"]) for p in self.db.products()}
    def movement_product_display(self,product):return f"{product_label(product)}  [{product['unit']}]"
    def movement_product_results(self,query=""):
        return self.db.products(query)
    def hide_product_suggestions(self):
        if hasattr(self,"product_suggestions"):
            self.product_suggestions.pack_forget();self.product_suggestions_collapsed=True
            self.product_suggestions_toggle.configure(image=self.icons["expand"])
    def toggle_product_suggestions(self):
        self.product_suggestions_collapsed=not self.product_suggestions_collapsed
        self.product_suggestions_toggle.configure(image=self.icons["expand" if self.product_suggestions_collapsed else "collapse"])
        if self.product_suggestions_collapsed:self.hide_product_suggestions()
        else:self.show_product_suggestions();self.m_product_entry.focus_set()
        self.save_interface_state()
    def show_product_suggestions(self, force=False):
        if not hasattr(self,"product_suggestions"):return
        if force:
            self.product_suggestions_collapsed=False;self.product_suggestions_toggle.configure(image=self.icons["collapse"])
        if self.product_suggestions_collapsed:
            self.hide_product_suggestions();return
        for child in self.product_suggestions.winfo_children():child.destroy()
        results=self.movement_product_results(self.m_product.get())
        if not results:
            ctk.CTkLabel(self.product_suggestions,text="Nenhum produto encontrado",height=36,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(fill="x",padx=10,pady=4)
        else:
            for product in results[:SEARCH_RESULT_LIMIT]:
                label=self.movement_product_display(product)
                ctk.CTkButton(self.product_suggestions,text=label,anchor="w",height=34,corner_radius=6,fg_color="transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_movement_product(product_id)).pack(fill="x",padx=5,pady=2)
            if len(results)>SEARCH_RESULT_LIMIT:ctk.CTkLabel(self.product_suggestions,text=f"Mostrando {SEARCH_RESULT_LIMIT} de {len(results)}. Continue digitando para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)
        self.product_suggestions.pack(fill="x",pady=(0,10),before=self.draft_tree)
    def on_product_search(self,event=None):
        if event and event.keysym in ("Return","Escape"):
            if event.keysym=="Escape":self.hide_product_suggestions()
            return
        selected=self.db.product(self.m_selected_product_id) if self.m_selected_product_id else None
        if not selected or self.m_product.get()!=self.movement_product_display(selected):self.m_selected_product_id=None;self.update_current_stock()
        self.schedule_ui_task("movement_product_search",lambda:self.show_product_suggestions(force=True),100)
    def select_first_product_suggestion(self):
        results=self.movement_product_results(self.m_product.get())
        if results:self.select_movement_product(int(results[0]["id"]));self.m_quantity_entry.focus_set()
    def select_movement_product(self,product_id):
        self.cancel_ui_task("movement_product_search")
        product=self.db.product(int(product_id))
        if not product:return
        self.m_selected_product_id=int(product_id);self.m_product.set(self.movement_product_display(product));self.hide_product_suggestions();self.update_current_stock()
    def operation_map(self, include_inactive=False, include_internal=False):return {str(operation["name"]):int(operation["id"]) for operation in self.db.operations(include_inactive=include_inactive,include_internal=include_internal)}
    def user_names(self, include_inactive=False):return [str(user["name"]) for user in self.db.users(include_inactive=include_inactive)]
    def refresh_user_controls(self):
        names=self.user_names();values=names or ["Cadastre um usuário na aba Cadastro"]
        if hasattr(self,"m_user_menu"):
            self.m_user_menu.configure(values=values)
            if self.m_user.get() not in names:self.m_user.set(names[0] if names else values[0])
        if hasattr(self,"c_responsible_menu"):
            self.c_responsible_menu.configure(values=values)
            if self.c_responsible.get() not in names:self.c_responsible.set(names[0] if names else values[0])
        if hasattr(self,"quick_user_menu"):
            self.quick_user_menu.configure(values=values)
            if self.quick_user.get() not in names:self.quick_user.set(names[0] if names else values[0])
    def refresh_operation_controls(self):
        active = self.operation_map()
        if hasattr(self, "m_operation_menu"):
            values = list(active) or [""]
            self.m_operation_menu.configure(values=values)
            if self.m_operation.get() not in active:
                self.m_operation.set("Entrada" if "Entrada" in active else values[0])
            self.operation_mapping = active
            self.on_operation_change()
        if hasattr(self, "history_filter_menu"):
            all_operations = self.operation_map(include_inactive=True,include_internal=True)
            self.history_operation_mapping = {"Todas as operações": "todos", **all_operations}
            self.history_filter_menu.configure(values=list(self.history_operation_mapping))
            if self.history_filter.get() not in self.history_operation_mapping:
                self.history_filter.set("Todas as operações")
    def open_operation_manager(self):
        dialog=OperationManagerDialog(self);self.wait_window(dialog);self.refresh_operation_controls();self.refresh_movements()
    def on_operation_change(self):
        if not hasattr(self, "m_quantity_entry"):return
        operation=self.db.operation(getattr(self, "operation_mapping", {}).get(self.m_operation.get(), 0))
        self.m_quantity_entry.configure(placeholder_text="Nova contagem" if operation and operation["effect"]=="set" else "Quantidade")
        self.save_interface_state()
    def update_current_stock(self):
        pid=self.m_selected_product_id;self.current_stock.configure(text=f"Saldo atual: {fmt_number(self.db.stock(pid))}" if pid else "Saldo atual: —")
    def add_draft_item(self):
        pid=self.m_selected_product_id
        if not pid:
            results=self.movement_product_results(self.m_product.get())
            if len(results)==1:pid=int(results[0]["id"]);self.select_movement_product(pid)
            else:messagebox.showwarning(APP_NAME,"Escolha um produto na lista de resultados.",parent=self);self.show_product_suggestions();return
        try:amount=float(self.m_quantity.get().replace(",","."))
        except ValueError:messagebox.showwarning(APP_NAME,"Informe uma quantidade válida.",parent=self);return
        operation=self.db.operation(getattr(self,"operation_mapping",{}).get(self.m_operation.get(),0))
        if amount<0 or (operation and operation["effect"]!="set" and amount<=0):messagebox.showwarning(APP_NAME,"A quantidade deve ser maior que zero.",parent=self);return
        existing=next((index for index,item in enumerate(self.movement_draft) if item["product_id"]==pid and index!=self.draft_edit_index),None)
        if existing is not None:messagebox.showwarning(APP_NAME,"Esse produto já está no conjunto. Edite o item existente.",parent=self);return
        item={"product_id":pid,"quantity":amount}
        if self.draft_edit_index is None:self.movement_draft.append(item)
        else:self.movement_draft[self.draft_edit_index]=item
        self.draft_edit_index=None;self.m_selected_product_id=None;self.m_product.set("");self.m_quantity.set("");self.m_add_button.configure(text="Adicionar");self.update_current_stock();self.refresh_draft()
    def refresh_draft(self):
        if not hasattr(self,"draft_tree"):return
        self.draft_tree.delete(*self.draft_tree.get_children());mapping=self.product_map();reverse={value:key for key,value in mapping.items()}
        for index,item in enumerate(self.movement_draft):
            label=reverse.get(item["product_id"],"Produto removido");self.draft_tree.insert("","end",iid=str(index),values=(label,fmt_number(item["quantity"])))
        count=len(self.movement_draft);total=sum(float(item["quantity"]) for item in self.movement_draft);action="Atualizar movimentação" if self.editing_batch_id else "Salvar movimentação"
        self.m_draft_total_label.configure(text=f"Quantidade total: {fmt_number(total)} {'item' if abs(total)==1 else 'itens'}")
        self.m_save_button.configure(text=f"{action} ({count} {'produto' if count==1 else 'produtos'})",state="normal" if count else "disabled")
        if self.editing_batch_id:
            if not self.m_cancel_edit_button.winfo_manager():self.m_cancel_edit_button.pack(side="left",padx=(8,0))
        else:self.m_cancel_edit_button.pack_forget()
    def edit_draft_item(self):
        selected=self.draft_tree.selection()
        if not selected:messagebox.showinfo(APP_NAME,"Selecione um item do conjunto para editar.",parent=self);return
        index=int(selected[0]);item=self.movement_draft[index];self.draft_edit_index=index;self.select_movement_product(item["product_id"]);self.m_quantity.set(fmt_number(item["quantity"]));self.m_add_button.configure(text="Atualizar");self.update_current_stock()
    def remove_draft_item(self):
        selected=self.draft_tree.selection()
        if not selected:messagebox.showinfo(APP_NAME,"Selecione um item do conjunto para remover.",parent=self);return
        del self.movement_draft[int(selected[0])];self.draft_edit_index=None;self.m_quantity.set("");self.m_add_button.configure(text="Adicionar");self.refresh_draft()
    def register_movement(self):
        operation_id=getattr(self,"operation_mapping",{}).get(self.m_operation.get())
        if not operation_id:messagebox.showwarning(APP_NAME,"Selecione uma operação.",parent=self);return
        if self.m_user.get() not in self.user_names():messagebox.showwarning(APP_NAME,"Cadastre e selecione um usuário responsável na aba Cadastro.",parent=self);return
        editing_batch_id=self.editing_batch_id
        try:
            movement_date=self.m_date_entry.get_date();items=[(item["product_id"],item["quantity"]) for item in self.movement_draft]
            if editing_batch_id:self.db.update_movement_batch(editing_batch_id,operation_id,items,movement_date.isoformat(),self.m_reason.get().strip(),self.m_user.get())
            else:self.db.add_movement_batch(operation_id,items,movement_date.isoformat(),self.m_reason.get().strip(),self.m_user.get())
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        count=len(self.movement_draft);self.editing_batch_id=None;self.movement_draft.clear();self.draft_edit_index=None;self.m_selected_product_id=None;self.m_product.set("");self.m_quantity.set("");self.m_reason.set("");self.m_date_entry.set_date(date.today());self.refresh_draft();self.refresh_all();self.update_current_stock()
        if editing_batch_id:self.show_movement_section("history")
        action="atualizada" if editing_batch_id else "registrada"
        self.show_movement_result(f"Movimentação {action} com {count} {'produto' if count==1 else 'produtos'}.")

    def cancel_batch_edit(self):
        self.editing_batch_id=None;self.movement_draft.clear();self.draft_edit_index=None;self.m_selected_product_id=None
        self.m_product.set("");self.m_quantity.set("");self.m_reason.set("");self.m_date_entry.set_date(date.today());self.m_add_button.configure(text="Adicionar")
        self.refresh_draft();self.update_current_stock()

    def load_movement_batch_for_edit(self, batch_id: int):
        batch=self.db.movement_batch(batch_id);items=self.db.movement_batch_items(batch_id)
        if not batch or not items:messagebox.showwarning(APP_NAME,"Essa movimentação não existe mais.",parent=self);self.refresh_movements();return
        self.refresh_operation_controls();operation=self.db.operation(int(batch["operation_id"]));operation_name=str(operation["name"]) if operation and operation["active"] else ""
        if not operation_name:
            replacement=next((item for item in self.db.operations() if item["effect"]==batch["operation_effect"]),None)
            if not replacement:messagebox.showwarning(APP_NAME,"Cadastre ou reative uma operação compatível antes de editar esta movimentação.",parent=self);return
            operation_name=str(replacement["name"])
            messagebox.showinfo(APP_NAME,f"A operação antiga não está mais ativa. Selecionei “{operation_name}” para você revisar antes de salvar.",parent=self)
        self.editing_batch_id=batch_id;self.m_operation.set(operation_name);self.on_operation_change()
        self.movement_draft=[]
        for item in items:
            informed=abs(float(item["quantity"])) if item["type"] in ("entrada","saida") else float(item["informed_quantity"] if item["informed_quantity"] is not None else item["resulting_stock"])
            self.movement_draft.append({"product_id":int(item["product_id"]),"quantity":informed})
        self.m_reason.set(batch["reason"] or "");self.m_date_entry.set_date(datetime.strptime(batch["movement_date"],"%Y-%m-%d").date())
        active_users=self.user_names();self.m_user.set(batch["performed_by"] if batch["performed_by"] in active_users else (active_users[0] if active_users else ""))
        self.draft_edit_index=None;self.m_selected_product_id=None;self.m_product.set("");self.m_quantity.set("");self.m_add_button.configure(text="Adicionar")
        self.show_movement_section("new");self.refresh_draft();self.update_current_stock()

    def show_movement_result(self, success_message: str):
        negative_products=self.db.negative_stock_products()
        if not negative_products:messagebox.showinfo(APP_NAME,success_message,parent=self);return
        details="\n".join(f"• {product_label(product)}: {fmt_number(product['stock'])} {product['unit']}" for product in negative_products)
        messagebox.showwarning(APP_NAME,f"{success_message}\n\nATENÇÃO: ESTOQUE NEGATIVO\n{details}\n\nA alteração foi concluída sem bloquear o saldo negativo. Verifique o ocorrido e registre uma entrada ou um ajuste positivo para corrigir o saldo.",parent=self)
    def selected_history_entry(self):
        selected=self.history_tree.selection();return selected[0] if selected else None

    def open_history_details(self):
        history_key=self.selected_history_entry()
        if not history_key:messagebox.showinfo(APP_NAME,"Selecione uma movimentação para abrir.",parent=self);return
        try:dialog=MovementHistoryDialog(self,history_key)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);self.refresh_movements();return
        self.wait_window(dialog)
        if dialog.action=="edit":self.edit_history_entry(history_key)
        elif dialog.action=="delete":self.delete_history_entry(history_key)

    def edit_history_entry(self, history_key: str):
        kind,raw_id=history_key.split(":",1);record_id=int(raw_id)
        if kind=="batch":
            if self.db.kit_conversion_batch(record_id):self.load_kit_conversion_for_edit(record_id)
            else:self.load_movement_batch_for_edit(record_id)
            return
        movement=self.db.movement(record_id)
        if not movement:messagebox.showwarning(APP_NAME,"Essa movimentação não existe mais.",parent=self);self.refresh_movements();return
        dialog=MovementDialog(self,movement);self.wait_window(dialog)
        if not dialog.result:return
        try:self.db.update_movement(record_id,**dialog.result)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.update_current_stock();self.show_movement_result("Movimentação atualizada.")

    def delete_history_entry(self, history_key: str):
        kind,raw_id=history_key.split(":",1);record_id=int(raw_id)
        if kind=="batch":
            batch=self.db.movement_batch(record_id);items=self.db.movement_batch_items(record_id)
            if not batch or not items:messagebox.showwarning(APP_NAME,"Essa movimentação não existe mais.",parent=self);self.refresh_movements();return
            movement_date=datetime.strptime(batch["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y")
            question=f"Excluir a movimentação #{record_id}, de {movement_date}, com {len(items)} {'produto' if len(items)==1 else 'produtos'}?\n\nTodo o conjunto será excluído e os saldos serão recalculados."
        else:
            movement=self.db.movement(record_id)
            if not movement:messagebox.showwarning(APP_NAME,"Essa movimentação não existe mais.",parent=self);self.refresh_movements();return
            question=f"Excluir a movimentação de {product_label(movement)}?\n\nO saldo do produto será recalculado."
        if not messagebox.askyesno(APP_NAME,question,icon="warning",parent=self):return
        try:
            if kind=="batch":self.db.delete_movement_batch(record_id)
            else:self.db.delete_movement(record_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.update_current_stock();self.show_movement_result("Movimentação excluída.")

    def refresh_movements(self):
        if not hasattr(self,"history_tree"):return
        self.refresh_user_controls()
        if self.m_selected_product_id and not self.db.product(self.m_selected_product_id):self.m_selected_product_id=None;self.m_product.set("");self.update_current_stock()
        self.refresh_operation_controls()
        if self.current_page!="movements" or self.settings.get("movement_section","new")!="history":return
        self.history_tree.delete(*self.history_tree.get_children())
        operation_filter=getattr(self,"history_operation_mapping",{}).get(self.history_filter.get(),"todos")
        for movement in self.db.movement_history(operation_filter):
            item_count=int(movement["item_count"]);item_label=f"{item_count} {'produto' if item_count==1 else 'produtos'}"
            self.history_tree.insert("","end",iid=movement["history_key"],values=(datetime.strptime(movement["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y"),movement["operation_name"],item_label,movement["product_summary"],movement["checked_by"]or"—",movement["reason"]or"Sem observação"))

    def refresh_movement_page(self):
        self.refresh_movements()
        if self.settings.get("movement_section","new")=="new" and not self.product_suggestions_collapsed:
            self.schedule_ui_task("movement_product_search",self.show_product_suggestions,30)

    def settings_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Configurações","Personalize a aparência e proteja seus dados.").pack(fill="x",pady=(0,22))
        appearance=Card(page);appearance.pack(fill="x",pady=(0,16));row=ctk.CTkFrame(appearance,fg_color="transparent");row.pack(fill="x",padx=22,pady=20);ctk.CTkLabel(row,text="Tema da interface",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w");ctk.CTkLabel(row,text="Escolha entre o modo claro off-white e o modo escuro em grafite.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",pady=(4,4));ctk.CTkLabel(row,text="Tema, janela, última tela e filtros ficam somente neste usuário do Windows e não são enviados ao Supabase.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",pady=(0,12));self.theme_selector=ctk.CTkSegmentedButton(row,values=["Light","Dark"],command=self.change_theme,selected_color=COLORS["accent"],selected_hover_color=COLORS["accent_hover"]);self.theme_selector.pack(anchor="w");self.theme_selector.set(self.settings.get("theme","Light"))
        cloud=Card(page);cloud.pack(fill="x",pady=(0,16));cloud_row=ctk.CTkFrame(cloud,fg_color="transparent");cloud_row.pack(fill="x",padx=22,pady=20)
        cloud_text=ctk.CTkFrame(cloud_row,fg_color="transparent");cloud_text.pack(side="left",fill="x",expand=True)
        ctk.CTkLabel(cloud_text,text="Supabase — cópia na nuvem",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w")
        self.cloud_status=tk.StringVar();ctk.CTkLabel(cloud_text,textvariable=self.cloud_status,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",pady=(4,0));self.update_cloud_status()
        cloud_actions=ctk.CTkFrame(cloud_row,fg_color="transparent");cloud_actions.pack(side="right")
        ctk.CTkButton(cloud_actions,text="Conta",width=90,height=38,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.cloud_account).pack(side="left",padx=4)
        ctk.CTkButton(cloud_actions,text="Enviar dados",width=115,height=38,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.cloud_upload).pack(side="left",padx=4)
        ctk.CTkButton(cloud_actions,text="Baixar dados",width=115,height=38,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.cloud_download).pack(side="left",padx=4)
        actions=ctk.CTkFrame(page,fg_color="transparent");actions.pack(fill="both",expand=True);actions.grid_columnconfigure((0,1),weight=1)
        for index,(title,text,icon_name,command,button) in enumerate((("Atualizações",f"Versão instalada: {APP_VERSION}. Verificação automática ao abrir.","refresh",self.check_updates,"Baixar e instalar atualização"),("Backup dos dados","Salve uma cópia segura do banco local.","download",self.backup,"Baixar backup"),("Restaurar backup","Substitua os dados por um backup anterior.","upload",self.restore,"Restaurar backup"))):
            card=Card(actions);card.grid(row=index//2,column=index%2,sticky="nsew",padx=(0 if index%2==0 else 8,8 if index%2==0 else 0),pady=8);ctk.CTkLabel(card,text=title,image=self.icons[icon_name],compound="left",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(anchor="w",padx=20,pady=(20,5));ctk.CTkLabel(card,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=20)
            action_button=ctk.CTkButton(card,text=button,height=38,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=command);action_button.pack(anchor="w",padx=20,pady=(12,20))
            if title=="Atualizações":
                self.update_button=action_button;self.update_status=tk.StringVar(value="O aplicativo procura novas versões sem interromper seu trabalho.")
                ctk.CTkLabel(card,textvariable=self.update_status,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w").pack(fill="x",padx=20,before=action_button)
        return page

    def update_cloud_status(self):
        if hasattr(self,"cloud_status"):
            self.cloud_status.set(f"Conectado como {self.cloud.email} — estoque compartilhado e automático" if self.cloud.signed_in else "Desconectado — entre ou crie sua conta segura")

    def cloud_account(self):
        if self.cloud.signed_in:
            if messagebox.askyesno(APP_NAME,f"Sair da conta {self.cloud.email}?",parent=self):
                self.cloud.sign_out();self.save_cloud_settings();self.update_cloud_status()
        else:CloudLoginDialog(self)

    def cloud_upload(self):
        if not self.cloud.signed_in:CloudLoginDialog(self);return
        if not messagebox.askyesno(APP_NAME,"Enviar agora os produtos, movimentações, cadastros, vínculos de SKU e fotos para o estoque compartilhado no Supabase?",parent=self):return
        try:self.cloud.upload(self.db.db);self.save_cloud_settings()
        except CloudSyncError as error:messagebox.showerror(APP_NAME,str(error),parent=self);return
        messagebox.showinfo(APP_NAME,"Dados enviados e protegidos no Supabase.",parent=self)

    def cloud_download(self):
        if not self.cloud.signed_in:CloudLoginDialog(self);return
        if not messagebox.askyesno(APP_NAME,"Baixar a cópia do Supabase e substituir os dados locais?\n\nUm backup local de segurança será criado automaticamente.",icon="warning",parent=self):return
        try:updated_at=self.cloud.download(self.db.db);self.save_cloud_settings()
        except (CloudSyncError,KeyError,ValueError,sqlite3.Error,OSError) as error:messagebox.showerror(APP_NAME,f"Não foi possível baixar os dados.\n\n{error}",parent=self);return
        self.refresh_all();messagebox.showinfo(APP_NAME,f"Dados restaurados da nuvem.\nCópia remota: {updated_at[:19].replace('T',' ')}",parent=self)

    def schedule_cloud_sync(self):
        if not hasattr(self,"cloud") or not self.cloud.signed_in:return
        self.cloud_settings["cloud_local_modified_at"]=datetime.now(timezone.utc).isoformat()
        if getattr(self,"cloud_sync_timer",None) is not None:
            try:self.after_cancel(self.cloud_sync_timer)
            except (tk.TclError,ValueError):pass
        self.cloud_sync_timer=self.after(1800,lambda:self.start_cloud_sync(silent=True))

    def start_cloud_sync(self,silent=True,prefer_local=False):
        if not self.cloud.signed_in:return
        if self.cloud_sync_busy:
            self.cloud_sync_pending=True;return
        self.cloud_sync_busy=True;self.cloud_sync_pending=False;self.cloud_sync_timer=None
        if hasattr(self,"cloud_status"):self.cloud_status.set(f"Conectado como {self.cloud.email} — sincronizando...")
        def worker():
            connection=sqlite3.connect(self.db.path,timeout=5)
            configure_database_connection(connection)
            try:self.cloud_events.put(("success",self.cloud.synchronize(connection,prefer_local),silent))
            except CloudSyncError as error:self.cloud_events.put(("error",str(error),silent))
            except (KeyError,ValueError,sqlite3.Error,OSError) as error:self.cloud_events.put(("error",f"Não foi possível sincronizar: {error}",silent))
            finally:connection.close()
        threading.Thread(target=worker,daemon=True).start();self.after(120,self.poll_cloud_sync_events)

    def poll_cloud_sync_events(self):
        try:event=self.cloud_events.get_nowait()
        except queue.Empty:
            if self.cloud_sync_busy:self.after(120,self.poll_cloud_sync_events)
            return
        kind,result,silent=event
        self.cloud_sync_busy=False;self.save_cloud_settings();self.update_cloud_status()
        if kind=="success":
            action=result.get("action")
            if action=="downloaded":self.refresh_all()
            if not silent:
                messages={"uploaded":"Dados locais enviados ao estoque compartilhado.","downloaded":"Este computador recebeu os dados mais recentes dos outros usuários.","unchanged":"Todos os usuários já estão sincronizados."}
                messagebox.showinfo(APP_NAME,messages.get(action,"Sincronização concluída."),parent=self)
        elif not silent:messagebox.showerror(APP_NAME,result,parent=self)
        if self.cloud_sync_pending:self.after(300,lambda:self.start_cloud_sync(silent=True))

    def periodic_cloud_sync(self):
        if self.cloud.signed_in:self.start_cloud_sync(silent=True)
        self.after(20000,self.periodic_cloud_sync)

    def change_theme(self,value):
        self.settings["theme"]=value;self.save_interface_state();ctk.set_appearance_mode(value);self.configure_tables()
        if hasattr(self,"stock_confidence_cells"):self.stock_confidence_cells.schedule()
        if hasattr(self,"stock_quantity_cells"):self.stock_quantity_cells.schedule()
        if hasattr(self,"simulation_current_cells"):self.simulation_current_cells.schedule()
        if hasattr(self,"simulation_projected_cells"):self.simulation_projected_cells.schedule()
        if hasattr(self,"count_confidence_cells"):self.count_confidence_cells.schedule()
        if hasattr(self,"count_age_cells"):self.count_age_cells.schedule()
    def set_update_status(self,text):
        if hasattr(self,"update_status"):self.update_status.set(text)
    def reset_update_button(self):
        self.update_busy=False
        if self.update_button is not None:self.update_button.configure(state="normal",text="Baixar e instalar atualização")
    def check_updates(self,silent=False):
        if self.update_busy:
            if not silent:messagebox.showinfo(APP_NAME,"A verificação já está em andamento.",parent=self)
            return
        self.update_busy=True;self.set_update_status("Consultando a publicação oficial no GitHub...")
        if self.update_button is not None:self.update_button.configure(state="disabled",text="Verificando...")
        def worker():
            try:self.update_events.put(("checked",check_for_update(APP_VERSION,GITHUB_REPO),silent))
            except UpdateError as error:self.update_events.put(("check_error",str(error),silent))
            except Exception:self.update_events.put(("check_error","Falha inesperada ao buscar atualização.",silent))
        threading.Thread(target=worker,daemon=True).start();self.after(100,self.poll_update_events)
    def poll_update_events(self):
        try:event=self.update_events.get_nowait()
        except queue.Empty:
            if self.update_busy:self.after(100,self.poll_update_events)
            return
        kind=event[0]
        if kind=="progress":
            downloaded,total=event[1],event[2];percent=min(100,int(downloaded*100/total)) if total else 0
            self.set_update_status(f"Baixando atualização... {percent}%")
            if self.update_button is not None:self.update_button.configure(text=f"Baixando {percent}%")
        elif kind=="checked":self.update_check_finished(event[1],event[2])
        elif kind=="check_error":self.update_failed(event[1],event[2])
        elif kind=="downloaded":self.install_downloaded_update(event[1],event[2])
        elif kind=="download_error":self.update_failed(event[1],False)
        if self.update_busy:self.after(100,self.poll_update_events)
    def update_check_finished(self,info,silent):
        self.reset_update_button()
        if info is None:
            self.set_update_status(f"Versão {APP_VERSION}: aplicativo atualizado.")
            if not silent:messagebox.showinfo(APP_NAME,f"Você já usa a versão mais recente ({APP_VERSION}).",parent=self)
            return
        self.set_update_status(f"Nova versão disponível: {info.version}")
        notes=(info.notes[:700]+"…") if len(info.notes)>700 else info.notes
        if messagebox.askyesno(APP_NAME,f"Nova versão {info.version} disponível.\n\n{notes}\n\nDeseja baixar, substituir a versão anterior e reiniciar agora?",parent=self):self.download_available_update(info)
    def download_available_update(self,info):
        self.update_busy=True;self.set_update_status("Preparando download seguro...")
        if self.update_button is not None:self.update_button.configure(state="disabled",text="Baixando 0%")
        def progress(downloaded,total):self.update_events.put(("progress",downloaded,total))
        def worker():
            try:self.update_events.put(("downloaded",download_update(info,progress),info))
            except UpdateError as error:self.update_events.put(("download_error",str(error)))
            except Exception:self.update_events.put(("download_error","Não foi possível concluir o download."))
        threading.Thread(target=worker,daemon=True).start()
    def install_downloaded_update(self,path,info):
        self.set_update_status("Download validado. Substituindo a versão anterior e reiniciando...")
        try:start_update_install(path,info.sha256)
        except (UpdateError,OSError) as error:self.update_failed(str(error),False);return
        self.after(150,self.close)
    def update_failed(self,error,silent):
        self.reset_update_button();self.set_update_status(error)
        if not silent:messagebox.showerror(APP_NAME,error,parent=self)
    def backup(self):
        target=filedialog.asksaveasfilename(parent=self,defaultextension=".db",initialfile=f"estoque-backup-{date.today()}.db",filetypes=[("Backup","*.db")]);
        if target:self.db.backup(Path(target));messagebox.showinfo(APP_NAME,"Backup salvo.",parent=self)
    def restore(self):
        source=filedialog.askopenfilename(parent=self,filetypes=[("Backup","*.db")]);
        if source and messagebox.askyesno(APP_NAME,"Substituir os dados atuais?",parent=self):self.db.restore(Path(source));self.refresh_all()
    def refresh_all(self):
        """Refresh only the visible page; hidden pages update when opened."""
        self.db.ensure_kit_operations()
        refresh_name={"stock":"refresh_stock","movements":"refresh_movement_page","defect_return":"refresh_defect_return_page","kit_conversion":"refresh_kit_conversion","simulation":"refresh_simulation_page","count":"refresh_counts"}.get(self.current_page)
        if refresh_name:
            refresh=getattr(self,refresh_name,None)
            if refresh:refresh()
    def close(self):
        try:
            state = self.state()
            if state in ("normal", "zoomed"): self._last_window_state = state
            if state == "normal" and parse_window_geometry(self.geometry()): self._normal_geometry = self.geometry()
            self.settings["window_state"] = self._last_window_state
            self.settings["window_geometry"] = visible_window_geometry(self._normal_geometry, self.work_areas, self.minimum_width, self.minimum_height)
            self.capture_interface_preferences();self.save_settings();self.save_cloud_settings()
        finally:
            self.db.db.close(); self.destroy()


if __name__=="__main__":
    try:
        if run_update_helper(sys.argv):raise SystemExit(0)
        schedule_update_cleanup(sys.argv);enable_dpi_awareness();EstoqueApp().mainloop()
    except Exception as error:(data_dir()/"erro.log").write_text(f"{datetime.now().isoformat()}\n{error!r}\n",encoding="utf-8");raise
