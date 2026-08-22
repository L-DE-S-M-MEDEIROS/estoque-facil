from __future__ import annotations

import ctypes
import math
import queue
import re
import shutil
import sqlite3
import sys
import threading
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import ImageTk

from premium_icons import app_icon, application_icon_path, brand_mark, icon
from premium_widgets import MaskedDateEntry, TreeConfidenceOverlay, TreeRelativeDateOverlay, TreeRowSeparatorOverlay, TreeStockOverlay, confidence_tier
from cloud_sync import CloudSync, CloudSyncError
from local_state import LocalCloudSession, LocalPreferences, LocalSimulationDraft, read_json_object
from sales_list_import import SalesListError, normalize_sku_key, read_sales_list
from updater import UpdateError, check_for_update, download_update, run_update_helper, schedule_update_cleanup, start_update_install

APP_NAME = "ESTOQUE BOLSAS BABY"
APP_VERSION = "1.1.12"
GITHUB_REPO = "L-DE-S-M-MEDEIROS/estoque-facil"

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
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def product_matches_search(product: sqlite3.Row, query: str) -> bool:
    """Match a product by partial name, group, variation or category."""
    needle = normalize_search_text(query).strip()
    if not needle:
        return True
    searchable = " ".join(
        str(product[field] or "")
        for field in ("name", "group_name", "variant", "category")
    )
    return needle in normalize_search_text(searchable)


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
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
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
        created_at = datetime.now().isoformat(timespec="seconds")
        for name, effect, legacy_type in (
            ("Entrada", "positive", "entrada"),
            ("Saída", "negative", "saida"),
            ("Ajuste", "set", "ajuste"),
            ("Inventário", "set", "inventario"),
        ):
            self.db.execute("""INSERT OR IGNORE INTO operation_types(name,effect,legacy_type,active,protected,created_at)
                VALUES(?,?,?,1,1,?)""", (name, effect, legacy_type, created_at))
        self.db.execute("""UPDATE movements SET operation_id=(
            SELECT id FROM operation_types WHERE legacy_type=movements.type
        ) WHERE operation_id IS NULL""")
        self.db.commit()
        self.on_change = None
        self.db.set_trace_callback(self._track_change)

    def _track_change(self, statement: str) -> None:
        if self.on_change and re.match(r"^\s*(INSERT|UPDATE|DELETE)\b", statement, re.IGNORECASE):
            self.on_change()

    def operations(self, include_inactive: bool = False, custom_only: bool = False) -> list[sqlite3.Row]:
        conditions = []
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
        term = f"%{search.strip()}%"
        return self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id
            WHERE p.name LIKE ? OR p.category LIKE ? OR p.group_name LIKE ? OR p.variant LIKE ?
            GROUP BY p.id ORDER BY
                CASE WHEN TRIM(COALESCE(p.group_name,''))='' THEN 1 ELSE 0 END,
                p.group_name COLLATE NOCASE,p.name COLLATE NOCASE,p.variant COLLATE NOCASE""", (term, term, term, term)).fetchall()

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
        return self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id WHERE p.id=? GROUP BY p.id""", (product_id,)).fetchone()

    def save_product(self, values: dict, product_id: int | None = None) -> None:
        fields = (values["name"], values["category"], values["group_name"], values["variant"], values["unit"], values["minimum"], values["photo"], values["notes"])
        if product_id:
            self.db.execute("UPDATE products SET name=?,category=?,group_name=?,variant=?,unit=?,minimum=?,photo=?,notes=? WHERE id=?", fields + (product_id,))
        else:
            self.db.execute("INSERT INTO products(name,category,group_name,variant,unit,minimum,photo,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?)", fields + (datetime.now().isoformat(timespec="seconds"),))
        self.db.commit()

    def delete_product(self, product_id: int) -> bool:
        if self.db.execute("SELECT 1 FROM movements WHERE product_id=? LIMIT 1", (product_id,)).fetchone():
            return False
        self.db.execute("DELETE FROM products WHERE id=?", (product_id,)); self.db.commit(); return True

    def sku_mappings(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.db.execute("""SELECT sm.*,COUNT(smp.product_id) product_count
            FROM sku_mappings sm
            LEFT JOIN sku_mapping_products smp ON smp.mapping_id=sm.id
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

    def stock_confidence(self, product_id: int, current_stock: float | None = None, as_of: date | None = None) -> dict:
        """Estimate balance reliability from age and activity since the last physical count."""
        today = as_of or date.today()
        last_count = self.db.execute("""SELECT * FROM movements WHERE product_id=? AND type='inventario'
            ORDER BY movement_date DESC,created_at DESC,id DESC LIMIT 1""", (product_id,)).fetchone()
        if last_count:
            activity = self.db.execute("""SELECT quantity FROM movements WHERE product_id=? AND (
                movement_date>? OR (movement_date=? AND (created_at>? OR (created_at=? AND id>?)))
            ) ORDER BY movement_date,created_at,id""", (product_id, last_count["movement_date"], last_count["movement_date"], last_count["created_at"], last_count["created_at"], last_count["id"])).fetchall()
            anchor = datetime.strptime(last_count["movement_date"], "%Y-%m-%d").date()
            base = 100.0
        else:
            activity = self.db.execute("SELECT quantity FROM movements WHERE product_id=? ORDER BY movement_date,created_at,id", (product_id,)).fetchall()
            product = self.db.execute("SELECT created_at FROM products WHERE id=?", (product_id,)).fetchone()
            first = self.db.execute("SELECT MIN(movement_date) value FROM movements WHERE product_id=?", (product_id,)).fetchone()["value"]
            anchor = datetime.strptime(first or product["created_at"][:10], "%Y-%m-%d").date()
            base = 45.0

        days = max(0, (today - anchor).days)
        movement_count = len(activity)
        moved_units = sum(abs(float(row["quantity"])) for row in activity)
        balance = self.stock(product_id) if current_stock is None else float(current_stock)
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

    def movement(self, movement_id: int) -> sqlite3.Row | None:
        return self.db.execute("""SELECT m.*,p.name,p.group_name,p.variant,p.unit,
            COALESCE(o.name,CASE m.type WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' WHEN 'ajuste' THEN 'Ajuste' ELSE 'Inventário' END) operation_name
            FROM movements m JOIN products p ON p.id=m.product_id
            LEFT JOIN operation_types o ON o.id=m.operation_id WHERE m.id=?""", (movement_id,)).fetchone()

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
        self.db.close(); shutil.copy2(source, self.path); self.__init__()

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
        form = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
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
        photo = self.photo
        if photo and (not self.product or photo != self.product["photo"]):
            source = Path(photo)
            if source.exists():
                target = data_dir()/"fotos"/f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix.lower()}"; shutil.copy2(source, target); photo = str(target)
        group_name = "" if self.group_name.get() == "Sem grupo" else self.group_name.get().strip()
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
        if dialog.result:self.parent.db.save_product(dialog.result,int(product["id"]) if product else None);self.refresh();self.parent.refresh_all()
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
        scale = parent.ui_scale; width, height = round(820*scale), round(620*scale)
        self.geometry(f"{width}x{height}+{parent.winfo_x()+70}+{parent.winfo_y()+45}")
        self.minsize(round(680*scale), round(500*scale)); self.transient(parent); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)

        mapping = parent.db.sku_mapping(mapping_id) if mapping_id else None
        current_products = parent.db.sku_mapping_products(mapping_id) if mapping_id else []
        selected = {int(product["id"]) for product in current_products}
        self.product_variables = {int(product["id"]): tk.BooleanVar(value=int(product["id"]) in selected) for product in parent.db.products()}

        header = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0); header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="MEMÓRIA DE SKU", text_color=COLORS["accent"], font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(18,2))
        ctk.CTkLabel(header, text="Escolha os produtos descontados", text_color=COLORS["text"], font=ctk.CTkFont("Inter",22,"bold")).pack(anchor="w",padx=28)
        subtitle = context or "Cada unidade vendida deste SKU descontará uma unidade de cada produto marcado."
        ctk.CTkLabel(header, text=subtitle, text_color=COLORS["muted"], font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,18))

        content = ctk.CTkFrame(self, fg_color="transparent"); content.grid(row=1,column=0,sticky="nsew",padx=24,pady=20); content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(2,weight=1)
        sku_row = ctk.CTkFrame(content,fg_color="transparent"); sku_row.grid(row=0,column=0,sticky="ew",pady=(0,12)); sku_row.grid_columnconfigure(0,weight=1)
        self.sku = tk.StringVar(value=str(mapping["sku"] if mapping else sku))
        self.sku_entry = ctk.CTkEntry(sku_row,textvariable=self.sku,height=42,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"])
        self.sku_entry.grid(row=0,column=0,sticky="ew")
        if locked_sku:self.sku_entry.configure(state="disabled")
        self.search = ctk.CTkEntry(content,placeholder_text="Buscar produto, grupo, variação ou categoria...",height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"])
        self.search.grid(row=1,column=0,sticky="ew",pady=(0,10)); self.search.bind("<KeyRelease>",lambda _event:self.refresh_products())
        self.product_list = ctk.CTkScrollableFrame(content,fg_color=COLORS["surface"],corner_radius=10,border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.product_list.grid(row=2,column=0,sticky="nsew")
        actions=ctk.CTkFrame(content,fg_color="transparent");actions.grid(row=3,column=0,sticky="ew",pady=(14,0))
        ctk.CTkButton(actions,text="Cancelar",width=105,height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.destroy).pack(side="left")
        ctk.CTkButton(actions,text="Salvar vínculo",width=160,height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.save).pack(side="right")
        self.refresh_products()

    def refresh_products(self):
        for widget in self.product_list.winfo_children():widget.destroy()
        query=self.search.get().strip();visible=0
        for product in self.parent.db.products():
            if query and not product_matches_search(product,query):continue
            visible+=1;row=ctk.CTkFrame(self.product_list,fg_color="transparent");row.pack(fill="x",padx=8,pady=4)
            checkbox=ctk.CTkCheckBox(row,text=product_label(product),variable=self.product_variables[int(product["id"])],onvalue=True,offvalue=False,text_color=COLORS["text"],fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],font=ctk.CTkFont("Inter",11,"bold"))
            checkbox.pack(side="left",fill="x",expand=True,anchor="w")
            ctk.CTkLabel(row,text=f"Saldo: {fmt_number(product['stock'])} {product['unit']}",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(side="right",padx=10)
        if not visible:ctk.CTkLabel(self.product_list,text="Nenhum produto encontrado.",text_color=COLORS["muted"]).pack(pady=24)

    def save(self):
        selected=[product_id for product_id,variable in self.product_variables.items() if variable.get()]
        try:self.result=self.parent.db.save_sku_mapping(self.sku.get(),selected,self.mapping_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.destroy()


class SkuManagerDialog(BrandedToplevel):
    def __init__(self,parent:"EstoqueApp"):
        super().__init__(parent,fg_color=COLORS["background"]);self.parent=parent;self.title("Gerenciar vínculos de SKU")
        scale=parent.ui_scale;width,height=round(940*scale),round(590*scale);self.geometry(f"{width}x{height}+{parent.winfo_x()+55}+{parent.winfo_y()+55}");self.minsize(round(760*scale),round(480*scale));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text="SKUS DE VENDA",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(18,2));ctk.CTkLabel(header,text="Memória de produtos por SKU",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text="Consulte e altere quais produtos serão descontados nas próximas listas importadas.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,18))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=20);content.grid_columnconfigure(0,weight=1);content.grid_rowconfigure(1,weight=1)
        toolbar=ctk.CTkFrame(content,fg_color="transparent");toolbar.grid(row=0,column=0,sticky="ew",pady=(0,12))
        self.search=ctk.CTkEntry(toolbar,placeholder_text="Buscar SKU ou produto vinculado...",width=360,height=40,corner_radius=9);self.search.pack(side="left");self.search.bind("<KeyRelease>",lambda _event:self.refresh())
        ctk.CTkButton(toolbar,text="Novo SKU",image=parent.icons["plus"],height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.new_mapping).pack(side="right")
        ctk.CTkButton(toolbar,text="Excluir",image=parent.icons["trash"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_mapping).pack(side="right",padx=8)
        ctk.CTkButton(toolbar,text="Editar vínculo",image=parent.icons["edit"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_mapping).pack(side="right")
        card=Card(content);card.grid(row=1,column=0,sticky="nsew");self.tree=parent.table(card,("sku","products"),("SKU","Produtos descontados"),(260,570));self.tree.column("products",anchor="w");self.tree.pack(fill="both",expand=True,padx=18,pady=18);self.tree.bind("<Double-1>",lambda _event:self.edit_mapping());self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for mapping in self.parent.db.sku_mappings(self.search.get()):
            products=self.parent.db.sku_mapping_products(int(mapping["id"]));labels="; ".join(product_label(product) for product in products) or "Sem produtos"
            self.tree.insert("","end",iid=str(mapping["id"]),values=(mapping["sku"],labels))

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
        scale=parent.ui_scale;width,height=round(1020*scale),round(700*scale);self.geometry(f"{width}x{height}+{parent.winfo_x()+35}+{parent.winfo_y()+25}");self.minsize(round(820*scale),round(570*scale));self.transient(parent);self.grab_set();self.grid_columnconfigure(0,weight=1);self.grid_rowconfigure(1,weight=1)
        header=ctk.CTkFrame(self,fg_color=COLORS["surface"],corner_radius=0);header.grid(row=0,column=0,sticky="ew")
        ctk.CTkLabel(header,text=f"LISTA {source_label.upper()}",text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=28,pady=(18,2));ctk.CTkLabel(header,text="Confira antes de levar para Movimentações",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold")).pack(anchor="w",padx=28)
        ctk.CTkLabel(header,text=file_name,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=28,pady=(5,18))
        content=ctk.CTkFrame(self,fg_color="transparent");content.grid(row=1,column=0,sticky="nsew",padx=24,pady=18);content.grid_columnconfigure(0,weight=1);content.grid_rowconfigure((1,3),weight=1)
        sku_bar=ctk.CTkFrame(content,fg_color="transparent");sku_bar.grid(row=0,column=0,sticky="ew",pady=(0,8));ctk.CTkLabel(sku_bar,text="Leitura por SKU",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(side="left")
        ctk.CTkButton(sku_bar,text="Alterar vínculo selecionado",image=parent.icons["edit"],height=34,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_mapping).pack(side="right")
        self.sku_tree=parent.table(content,("sku","quantity","products"),("SKU","Qnt. da lista","Produtos descontados"),(270,110,560));self.sku_tree.column("products",anchor="w");self.sku_tree.grid(row=1,column=0,sticky="nsew")
        ctk.CTkLabel(content,text="Baixa consolidada por produto",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).grid(row=2,column=0,sticky="w",pady=(14,8))
        self.product_tree=parent.table(content,("product","quantity","current","after"),("Produto","Quantidade a descontar","Saldo atual","Saldo depois"),(500,160,130,130));self.product_tree.column("product",anchor="w");self.product_tree.grid(row=3,column=0,sticky="nsew")
        actions=ctk.CTkFrame(content,fg_color="transparent");actions.grid(row=4,column=0,sticky="ew",pady=(14,0));ctk.CTkButton(actions,text="Cancelar",width=105,height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.destroy).pack(side="left")
        ctk.CTkButton(actions,text="Adicionar à movimentação",width=220,height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.confirm).pack(side="right")
        self.refresh()

    def refresh(self):
        self.review_rows,self.product_rows=mapped_sales_list(self.parent.db,self.items);self.sku_tree.delete(*self.sku_tree.get_children());self.product_tree.delete(*self.product_tree.get_children())
        for index,row in enumerate(self.review_rows):self.sku_tree.insert("","end",iid=str(index),values=(row["sku"],fmt_number(row["quantity"]),"; ".join(row["products"])))
        for row in self.product_rows:
            product=row["product"];current=float(product["stock"]);after=current-float(row["quantity"])
            self.product_tree.insert("","end",iid=str(row["product_id"]),values=(product_label(product),f"{fmt_number(row['quantity'])} {product['unit']}",f"{fmt_number(current)} {product['unit']}",f"{fmt_number(after)} {product['unit']}"))

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
        form = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
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
        self.icons = {name: icon(name, 22) for name in ("products", "stock", "movements", "simulation", "count", "settings", "registration", "user", "operation", "group", "plus", "search", "edit", "trash", "download", "upload", "refresh", "collapse", "expand")}
        self.table_separators: list[TreeRowSeparatorOverlay] = []
        self.update_events: queue.Queue = queue.Queue(); self.update_busy = False; self.update_button = None
        self.cloud_events: queue.Queue = queue.Queue(); self.cloud_sync_busy = False; self.cloud_sync_pending = False; self.cloud_sync_timer = None
        self.nav_buttons = {}; self.pages = {}; self.current_page = ""; self.build_shell(); self.show_page(self.settings.get("last_page", "stock"))
        self.bind("<Configure>", self.remember_window_geometry)
        self.after_idle(self.restore_window)
        self.after(2500, lambda: self.check_updates(silent=True))
        self.after(4000, lambda: self.start_cloud_sync(silent=True))
        self.after(20000, self.periodic_cloud_sync)

    def save_settings(self): self.preferences_store.save(); self.settings = self.preferences_store.values

    def save_cloud_settings(self): self.cloud_session_store.values = self.cloud_settings; self.cloud_session_store.save(); self.cloud_settings = self.cloud_session_store.values

    def capture_interface_preferences(self):
        if self.current_page:self.settings["last_page"] = self.current_page
        if hasattr(self,"stock_search"):self.settings["stock_search"] = self.stock_search.get()
        if hasattr(self,"count_search"):self.settings["count_search"] = self.count_search.get()
        if hasattr(self,"count_filter"):self.settings["count_filter"] = self.count_filter.get()
        if hasattr(self,"m_operation"):self.settings["movement_operation"] = self.m_operation.get()
        if hasattr(self,"m_user"):self.settings["movement_user"] = self.m_user.get()
        if hasattr(self,"history_filter"):self.settings["history_filter"] = self.history_filter.get()
        if hasattr(self,"product_suggestions_collapsed"):self.settings["movement_products_expanded"] = not self.product_suggestions_collapsed

    def save_interface_state(self):self.capture_interface_preferences();self.save_settings()

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
        for key, label in (("stock","Estoque atual"),("movements","Movimentações"),("simulation","Simulação"),("count","Contagem"),("registration","Cadastro"),("settings","Configurações")):
            button = ctk.CTkButton(self.sidebar, text=label, image=self.icons[key], compound="left", anchor="w", height=48, corner_radius=10, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 13, "bold"), command=lambda k=key:self.show_page(k))
            button.pack(fill="x", padx=16, pady=4); self.nav_buttons[key]=button
        self.sidebar_status=ctk.CTkLabel(self.sidebar, text=f"●  Local + nuvem segura\n    Versão {APP_VERSION}", justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10));self.sidebar_status.pack(side="bottom", anchor="w", padx=26, pady=28)
        self.content = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0); self.content.grid(row=0,column=1,sticky="nsew"); self.content.grid_columnconfigure(0,weight=1); self.content.grid_rowconfigure(0,weight=1)

    def show_page(self,key):
        self.capture_interface_preferences()
        for page in self.pages.values(): page.grid_remove()
        if key not in self.pages: self.pages[key]={"registration":self.registration_page,"stock":self.stock_page,"movements":self.movements_page,"simulation":self.simulation_page,"count":self.count_page,"settings":self.settings_page}[key]()
        self.pages[key].grid(row=0,column=0,sticky="nsew",padx=32,pady=28)
        self.current_page=key;self.settings["last_page"]=key;self.save_settings()
        for name,button in self.nav_buttons.items():
            selected = name == key
            button.configure(
                fg_color=COLORS["nav_selected"] if selected else "transparent",
                text_color=COLORS["accent"] if selected else COLORS["muted"],
                border_width=1 if selected else 0,
                border_color=COLORS["accent"] if selected else COLORS["sidebar"],
            )
        {"registration":lambda:None,"stock":self.refresh_stock,"movements":self.refresh_movements,"simulation":self.refresh_simulation,"count":self.refresh_counts,"settings":lambda:None}[key]()

    def table(self,parent,columns,headings,widths):
        tree=ttk.Treeview(parent,columns=columns,show="headings",selectmode="browse")
        for col,label,width in zip(columns,headings,widths): tree.heading(col,text=label,anchor="center"); tree.column(col,width=width,anchor="center")
        separator=TreeRowSeparatorOverlay(tree,COLORS);tree._row_separator=separator;self.table_separators.append(separator)
        original_insert,original_delete=tree.insert,tree.delete
        def insert_with_separator(*args,**kwargs):
            item=original_insert(*args,**kwargs);separator.schedule();return item
        def delete_with_separator(*args,**kwargs):
            result=original_delete(*args,**kwargs);separator.schedule();return result
        tree.insert=insert_with_separator;tree.delete=delete_with_separator
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
        page=ctk.CTkScrollableFrame(self.content,fg_color="transparent",corner_radius=0,scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
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
        toolbar=ctk.CTkFrame(page,fg_color="transparent");toolbar.pack(fill="x",pady=(0,16)); self.product_search=ctk.CTkEntry(toolbar,placeholder_text="Buscar por produto, grupo ou variação...",width=430,height=44,corner_radius=10,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.product_search.pack(side="left");self.product_search.bind("<KeyRelease>",lambda e:self.refresh_products())
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
        if dialog.result:self.db.save_product(dialog.result);self.refresh_all()

    def edit_product(self):
        pid=self.selected_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para editar.",parent=self);return
        dialog=ProductDialog(self,self.db.product(pid));self.wait_window(dialog)
        if dialog.result:self.db.save_product(dialog.result,pid);self.refresh_all()

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
        card=Card(page);card.pack(fill="both",expand=True);bar=ctk.CTkFrame(card,fg_color="transparent");bar.pack(fill="x",padx=20,pady=(18,8));ctk.CTkLabel(bar,text="Posição do estoque",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(side="left");self.stock_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto ou grupo...",width=300,height=38,corner_radius=9);self.stock_search.pack(side="right");self.stock_search.insert(0,self.settings.get("stock_search",""));self.stock_search.bind("<KeyRelease>",lambda _event:(self.refresh_stock(),self.save_interface_state()))
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
        items=self.db.products(self.stock_search.get() if hasattr(self,"stock_search") else "");self.stock_tree.delete(*self.stock_tree.get_children());units=low=low_confidence=0;scores={};quantities={};current_group=None;group_index=0
        for p in items:
            group=(p["group_name"]or"").strip()or"Sem grupo";group_key=group.casefold()
            if group_key!=current_group:group_index+=1;current_group=group_key;self.stock_tree.insert("","end",iid=f"group:{group_index}",values=(f"—  {group.upper()}  —","","",""),tags=("group_header",))
            stock=float(p["stock"]);units+=stock;status="Negativo" if stock<0 else "Sem estoque" if stock==0 else "Estoque baixo" if stock<=float(p["minimum"]) else "Normal";low+=status!="Normal";trust=self.db.stock_confidence(int(p["id"]),stock);low_confidence+=trust["level"]=="Baixa";scores[int(p["id"])]=trust["score"];quantities[int(p["id"])]=(stock,fmt_number(stock));self.stock_tree.insert("","end",iid=str(p["id"]),values=("",p["name"],"",""))
        self.stock_confidence_cells.set_scores(scores)
        self.stock_quantity_cells.set_quantities(quantities)
        for label,text in zip(self.stock_cards,(str(len(items)),fmt_number(units),str(low),str(low_confidence))):label.configure(text=text)

    def simulation_page(self):
        page=ctk.CTkScrollableFrame(self.content,fg_color="transparent",corner_radius=0,scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
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
        self.sim_add_button=ctk.CTkButton(controls,text="Adicionar / atualizar",width=155,height=40,corner_radius=9,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.add_simulation_item)
        self.sim_add_button.grid(row=1,column=3,sticky="ew",padx=(8,20),pady=(0,8))
        self.sim_selected_stock=ctk.CTkLabel(controls,text="Selecione um produto para ver o saldo atual.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w")
        self.sim_selected_stock.grid(row=2,column=0,columnspan=4,sticky="ew",padx=20,pady=(0,8))
        self.sim_product_results=ctk.CTkScrollableFrame(controls,height=112,corner_radius=9,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.sim_product_results.grid(row=3,column=0,columnspan=4,sticky="ew",padx=20,pady=(0,18))

        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,14));self.simulation_cards=[]
        for title in ("Produtos simulados","Unidades na operação","Saldos projetados negativos"):
            card=Card(cards,height=88);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=16,pady=(13,2));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",20,"bold"));label.pack(anchor="w",padx=16);self.simulation_cards.append(label)

        self.simulation_negative_alert=ctk.CTkFrame(page,fg_color=("#F6E7EA","#3A0711"),corner_radius=12,border_width=2,border_color=("#5A0B1A","#8F2433"))
        ctk.CTkLabel(self.simulation_negative_alert,text="ATENÇÃO: SALDO PROJETADO NEGATIVO",text_color=("#5A0B1A","#FFB3BE"),font=ctk.CTkFont("Inter",12,"bold")).pack(anchor="w",padx=18,pady=(12,2))
        self.simulation_negative_text=ctk.CTkLabel(self.simulation_negative_alert,text="",justify="left",anchor="w",wraplength=920,text_color=("#5A0B1A","#FFF1F3"),font=ctk.CTkFont("Inter",10,"bold"));self.simulation_negative_text.pack(fill="x",padx=18,pady=(0,12))

        self.simulation_result_card=Card(page);self.simulation_result_card.pack(fill="both",expand=True)
        result_bar=ctk.CTkFrame(self.simulation_result_card,fg_color="transparent");result_bar.pack(fill="x",padx=20,pady=(16,10))
        ctk.CTkLabel(result_bar,text="Comparação simultânea — estoque atual x depois da simulação",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left")
        ctk.CTkButton(result_bar,text="Limpar simulação",width=125,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.clear_simulation).pack(side="right")
        ctk.CTkButton(result_bar,text="Remover",image=self.icons["trash"],width=100,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.remove_simulation_item).pack(side="right",padx=(0,6))
        ctk.CTkButton(result_bar,text="Editar",image=self.icons["edit"],width=92,height=34,fg_color="transparent",hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_simulation_item).pack(side="right",padx=(0,6))
        simulation_table=ctk.CTkFrame(self.simulation_result_card,fg_color="transparent");simulation_table.pack(fill="both",expand=True,padx=20,pady=(0,20));simulation_table.grid_columnconfigure(0,weight=1);simulation_table.grid_rowconfigure(0,weight=1)
        self.simulation_tree=self.table(simulation_table,("product","current","movement","projected"),("Produto","Estoque atual","Operação simulada","Depois da simulação"),(360,130,150,150));self.simulation_tree.configure(height=10);self.simulation_tree.grid(row=0,column=0,sticky="nsew");self.simulation_tree.bind("<Double-1>",lambda _event:self.edit_simulation_item())
        self.simulation_scrollbar=ctk.CTkScrollbar(simulation_table,orientation="vertical",command=self.simulation_tree.yview,button_color=COLORS["accent"],button_hover_color=COLORS["accent_hover"]);self.simulation_scrollbar.grid(row=0,column=1,sticky="ns",padx=(8,0));self.simulation_tree.configure(yscrollcommand=self.simulation_scrollbar.set)
        self.simulation_current_cells=TreeStockOverlay(self.simulation_tree,COLORS,column="current")
        self.simulation_projected_cells=TreeStockOverlay(self.simulation_tree,COLORS,column="projected")
        self.configure_tables();self.refresh_simulation_product_results();self.refresh_simulation()
        return page

    def simulation_operation_key(self):return "entrada" if self.sim_operation.get()=="Entrada" else "saida"
    def on_simulation_operation_change(self,_value=None):
        self.save_simulation_draft();self.refresh_simulation()
    def simulation_product_matches(self):
        query=self.sim_product.get() if hasattr(self,"sim_product") else ""
        selected=self.db.product(self.sim_selected_product_id) if getattr(self,"sim_selected_product_id",None) else None
        if selected and query==self.movement_product_display(selected):return [selected]
        return [product for product in self.db.products() if product_matches_search(product,query)]
    def on_simulation_product_search(self):
        selected=self.db.product(self.sim_selected_product_id) if self.sim_selected_product_id else None
        if not selected or self.sim_product.get()!=self.movement_product_display(selected):
            self.sim_selected_product_id=None;self.sim_selected_stock.configure(text="Selecione um produto para ver o saldo atual.",text_color=COLORS["muted"])
        self.refresh_simulation_product_results()
    def refresh_simulation_product_results(self):
        if not hasattr(self,"sim_product_results"):return
        for child in self.sim_product_results.winfo_children():child.destroy()
        results=self.simulation_product_matches()
        if not results:
            ctk.CTkLabel(self.sim_product_results,text="Nenhum produto encontrado",height=36,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(fill="x",padx=8,pady=3);return
        for product in results[:60]:
            current=float(product["stock"]);label=f"{product_label(product)}  •  Atual: {fmt_number(current)} {product['unit']}"
            selected=int(product["id"])==self.sim_selected_product_id
            ctk.CTkButton(self.sim_product_results,text=label,anchor="w",height=34,corner_radius=6,fg_color=COLORS["accent_soft"] if selected else "transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_simulation_product(product_id)).pack(fill="x",padx=5,pady=2)
        if len(results)>60:ctk.CTkLabel(self.sim_product_results,text=f"Mostrando 60 de {len(results)} produtos. Digite mais detalhes para filtrar.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",9)).pack(pady=5)
    def select_first_simulation_product(self):
        results=self.simulation_product_matches()
        if results:self.select_simulation_product(int(results[0]["id"]));self.sim_quantity_entry.focus_set()
    def select_simulation_product(self,product_id):
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
    def save_simulation_draft(self):
        if not hasattr(self,"simulation_items"):return
        self.simulation_store.values={"operation":self.simulation_operation_key(),"items":[dict(item) for item in self.simulation_items]};self.simulation_store.save()
    def refresh_simulation(self):
        if not hasattr(self,"simulation_tree"):return
        self.simulation_tree.delete(*self.simulation_tree.get_children());current_cells={};projected_cells={};negative=[];operation=self.simulation_operation_key();sign="+" if operation=="entrada" else "−";products=self.db.products();product_ids={int(product["id"]) for product in products}
        valid_items=[item for item in self.simulation_items if int(item["product_id"]) in product_ids]
        if len(valid_items)!=len(self.simulation_items):self.simulation_items=valid_items;self.save_simulation_draft()
        total=sum(float(item["quantity"]) for item in valid_items)
        for row in simulation_stock_comparison(products,valid_items,operation):
            product=row["product"];product_id=row["product_id"];current=row["current"];quantity=row["quantity"];projected=row["projected"]
            movement="—" if quantity is None else f"{sign}{fmt_number(quantity)} {product['unit']}"
            self.simulation_tree.insert("","end",iid=str(product_id),values=(product_label(product),"",movement,""));current_cells[product_id]=(current,f"{fmt_number(current)} {product['unit']}");projected_cells[product_id]=(projected,f"{fmt_number(projected)} {product['unit']}")
            if quantity is not None and projected<0:negative.append((product,projected))
        self.simulation_current_cells.set_quantities(current_cells);self.simulation_projected_cells.set_quantities(projected_cells)
        for label,text in zip(self.simulation_cards,(str(len(valid_items)),fmt_number(total),str(len(negative)))):label.configure(text=text,text_color=("#5A0B1A","#FFB3BE") if label is self.simulation_cards[2] and negative else COLORS["text"])
        if negative:
            details="\n".join(f"• {product_label(product)}: {fmt_number(projected)} {product['unit']}" for product,projected in negative);self.simulation_negative_text.configure(text=details)
            if not self.simulation_negative_alert.winfo_manager():self.simulation_negative_alert.pack(fill="x",pady=(0,14),before=self.simulation_result_card)
        elif self.simulation_negative_alert.winfo_manager():self.simulation_negative_alert.pack_forget()

    def count_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Contagem","Faça o check-in físico do estoque e recupere a confiança dos saldos.").pack(fill="x",pady=(0,18))
        ctk.CTkLabel(page,text="A confiança diminui conforme passam os dias e aumentam a quantidade e a frequência das movimentações desde a última contagem.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w").pack(fill="x",pady=(0,12))
        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,16));self.count_cards=[]
        for title in ("A conferir","Conferidos hoje","Diferenças hoje","Confiança média"):
            card=Card(cards,height=92);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=16,pady=(13,2));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",20,"bold"));label.pack(anchor="w",padx=16);self.count_cards.append(label)
        body=ctk.CTkFrame(page,fg_color="transparent");body.pack(fill="both",expand=True);body.grid_columnconfigure(1,weight=1);body.grid_rowconfigure(0,weight=1)
        registered_users=self.user_names();saved_counter=self.settings.get("counter_name","");counter_values=registered_users or ["Cadastre um usuário na aba Cadastro"];selected_counter=saved_counter if saved_counter in registered_users else counter_values[0]
        form=Card(body,width=330);form.grid(row=0,column=0,sticky="ns",padx=(0,16));form.grid_propagate(False);ctk.CTkLabel(form,text="Novo check-in",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(anchor="w",padx=20,pady=(16,10));self.c_product=tk.StringVar();self.c_quantity=tk.StringVar();self.c_responsible=tk.StringVar(value=selected_counter);self.c_note=tk.StringVar()
        def count_label(text):ctk.CTkLabel(form,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=20,pady=(0,4))
        count_label("Produto")
        self.c_product_combo=ctk.CTkOptionMenu(form,variable=self.c_product,values=[""],height=36,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"],command=lambda _v:self.update_count_current());self.c_product_combo.pack(fill="x",padx=20,pady=(0,8))
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
        ctk.CTkButton(bar,text="Contar",image=self.icons["count"],width=95,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.prepare_count).pack(side="right",padx=(0,8))
        ctk.CTkButton(bar,text="Explicar",width=90,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.explain_confidence).pack(side="right",padx=(0,8))
        self.count_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto...",width=165,height=36,corner_radius=9);self.count_search.pack(side="right",padx=(0,8));self.count_search.insert(0,self.settings.get("count_search",""));self.count_search.bind("<KeyRelease>",lambda _event:(self.refresh_counts(),self.save_interface_state()))
        self.count_tree=self.table(listing,("product","stock","checkin","date","responsible","confidence","difference"),("Produto","Estoque atual","Check-in","Última contagem","Responsável","Confiança","Diferença"),(170,75,75,120,80,85,85));self.count_tree.pack(fill="both",expand=True,padx=20,pady=(0,20));self.count_tree.bind("<Double-1>",lambda _e:self.prepare_count());self.count_confidence_cells=TreeConfidenceOverlay(self.count_tree,COLORS,activate=self.prepare_count);self.count_age_cells=TreeRelativeDateOverlay(self.count_tree,COLORS);self.configure_tables();return page

    def update_count_current(self):
        pid=self.product_map().get(self.c_product.get()) if hasattr(self,"c_product") else None
        if not hasattr(self,"count_current"):return
        product=self.db.product(pid) if pid else None
        self.count_current.configure(text=f"Saldo do sistema: {fmt_number(product['stock'])} {product['unit']}" if product else "Saldo do sistema: —")

    def selected_count_product(self):
        selected=self.count_tree.selection() if hasattr(self,"count_tree") else ();return int(selected[0]) if selected else None

    def prepare_count(self):
        pid=self.selected_count_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para contar.",parent=self);return
        label=next((text for text,product_id in self.product_map().items() if product_id==pid),"")
        self.c_product.set(label);self.update_count_current();self.c_quantity_entry.focus_set()

    def explain_confidence(self):
        pid=self.selected_count_product()
        if not pid:messagebox.showinfo(APP_NAME,"Selecione um produto para consultar a confiança.",parent=self);return
        product=self.db.product(pid);trust=self.db.stock_confidence(pid,float(product["stock"]));last=datetime.strptime(trust["last_date"],"%Y-%m-%d").strftime("%d/%m/%y") if trust["last_date"] else "nunca realizada"
        messagebox.showinfo(APP_NAME,f"{product_label(product)}\n\nConfiança: {trust['score']}% — {trust['level']}\nÚltima contagem: {last}\nTempo considerado: {trust['days']} dia(s)\nMovimentações desde a contagem: {trust['movement_count']}\nQuantidade movimentada: {fmt_number(trust['moved_units'])} {product['unit']}\n\nQuanto mais tempo, operações diárias e unidades movimentadas, maior a necessidade de uma nova conferência.",parent=self)

    def register_count(self):
        pid=self.product_map().get(self.c_product.get());responsible=self.c_responsible.get().strip()
        if not pid:messagebox.showwarning(APP_NAME,"Selecione um produto.",parent=self);return
        if responsible not in self.user_names():messagebox.showwarning(APP_NAME,"Selecione um usuário cadastrado como responsável pela contagem.",parent=self);return
        try:amount=float(self.c_quantity.get().replace(",","."));count_date=self.c_date_entry.get_date()
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        if amount<0:messagebox.showwarning(APP_NAME,"A quantidade contada não pode ser negativa.",parent=self);return
        previous=self.db.stock(pid);difference=amount-previous
        try:self.db.add_movement(pid,"inventario",amount,count_date.isoformat(),self.c_note.get().strip() or "Contagem física",checked_by=responsible)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.settings["counter_name"]=responsible;self.save_settings();self.c_quantity.set("");self.c_note.set("");self.c_date_entry.set_date(date.today());self.refresh_all();self.update_count_current();messagebox.showinfo(APP_NAME,f"Contagem confirmada.\nDiferença encontrada: {'+' if difference>0 else ''}{fmt_number(difference)}",parent=self)

    def refresh_counts(self):
        if not hasattr(self,"count_tree"):return
        mapping=self.product_map();self.c_product_combo.configure(values=list(mapping)or[""]);search=self.count_search.get() if hasattr(self,"count_search") else "";items=self.db.products(search);self.count_tree.delete(*self.count_tree.get_children());all_items=self.db.products();pending=counted_today=differences_today=total_score=0;today=date.today().isoformat();infos={};visible_scores={};visible_ages={}
        for p in all_items:
            trust=self.db.stock_confidence(int(p["id"]),float(p["stock"]));infos[int(p["id"])]=trust;pending+=trust["checkin"]=="PENDENTE";counted_today+=trust["last_date"]==today;differences_today+=trust["last_date"]==today and trust["last_difference"] is not None and abs(trust["last_difference"])>.0000001;total_score+=trust["score"]
        selected_filter=self.count_filter.get() if hasattr(self,"count_filter") else "todos"
        for p in items:
            trust=infos[int(p["id"])];
            if selected_filter=="pendentes" and trust["checkin"]!="PENDENTE":continue
            if selected_filter=="verificados" and trust["checkin"]!="VERIFICADO":continue
            last=relative_past_date(trust["last_date"]);difference="—" if trust["last_difference"] is None else f"{'+' if trust['last_difference']>0 else ''}{fmt_number(trust['last_difference'])} {p['unit']}";visible_scores[int(p["id"])]=trust["score"];visible_ages[int(p["id"])]=(trust["days"] if trust["last_date"] else None,last);self.count_tree.insert("","end",iid=str(p["id"]),values=(product_label(p),f"{fmt_number(p['stock'])} {p['unit']}",trust["checkin"],"",trust["checked_by"]or"—","",difference))
        self.count_confidence_cells.set_scores(visible_scores)
        self.count_age_cells.set_ages(visible_ages)
        average=round(total_score/len(all_items)) if all_items else 0
        for label,text in zip(self.count_cards,(str(pending),str(counted_today),str(differences_today),f"{average}%")):label.configure(text=text)

    def movements_page(self):
        page = ctk.CTkScrollableFrame(self.content, fg_color="transparent", corner_radius=0, scrollbar_button_color=COLORS["accent"], scrollbar_button_hover_color=COLORS["accent_hover"])
        PageTitle(page, "Movimentações", "Monte um conjunto de produtos, revise e registre tudo de uma vez.").pack(fill="x", pady=(0, 18))
        self.movement_draft: list[dict] = []
        self.draft_edit_index: int | None = None
        self.m_selected_product_id: int | None = None
        self.m_operation = tk.StringVar(value=self.settings.get("movement_operation","Entrada"))
        self.m_product = tk.StringVar()
        self.m_quantity = tk.StringVar()
        self.m_reason = tk.StringVar()
        self.m_user = tk.StringVar(value=self.settings.get("movement_user",""))
        self.product_suggestions_collapsed = not self.settings.get("movement_products_expanded",False)

        sales_import = Card(page); sales_import.pack(fill="x",pady=(0,16))
        sales_text=ctk.CTkFrame(sales_import,fg_color="transparent");sales_text.pack(fill="x",expand=True,padx=20,pady=(16,8))
        ctk.CTkLabel(sales_text,text="Baixa automática com lista",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w")
        ctk.CTkLabel(sales_text,text="Leia SKU e quantidade, confira os vínculos e leve a baixa para o conjunto abaixo.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",pady=(4,0))
        sales_actions=ctk.CTkFrame(sales_import,fg_color="transparent");sales_actions.pack(fill="x",padx=20,pady=(0,16))
        ctk.CTkButton(sales_actions,text="Importar Lista Shopee",image=self.icons["upload"],height=40,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=lambda:self.import_sales_list("shopee")).pack(side="left",padx=(0,8))
        ctk.CTkButton(sales_actions,text="Importar Lista Mercado Livre",image=self.icons["upload"],height=40,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda:self.import_sales_list("mercado_livre")).pack(side="left")

        composer = Card(page)
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
        add_row = ctk.CTkFrame(items, fg_color="transparent")
        add_row.pack(fill="x", pady=(0, 10)); add_row.grid_columnconfigure(0, weight=1)
        self.m_product_search=ctk.CTkFrame(add_row,height=42,corner_radius=9,fg_color=COLORS["surface"],border_width=2,border_color=COLORS["accent"])
        self.m_product_search.grid(row=0,column=0,sticky="ew",padx=(0,8));self.m_product_search.grid_columnconfigure(1,weight=1);self.m_product_search.grid_propagate(False)
        ctk.CTkLabel(self.m_product_search,text="",image=self.icons["search"],width=42).grid(row=0,column=0,sticky="nsew",padx=(9,2),pady=4)
        self.m_product_entry=ctk.CTkEntry(self.m_product_search,textvariable=self.m_product,placeholder_text="Buscar produto, grupo ou variação...",height=34,corner_radius=0,border_width=0,fg_color="transparent")
        self.m_product_entry.grid(row=0,column=1,sticky="nsew",padx=(0,2),pady=3);self.m_product_entry.bind("<FocusIn>",lambda _event:self.show_product_suggestions(force=True));self.m_product_entry.bind("<KeyRelease>",self.on_product_search);self.m_product_entry.bind("<Return>",lambda _event:self.select_first_product_suggestion())
        self.product_suggestions_toggle = ctk.CTkButton(self.m_product_search, text="", image=self.icons["expand" if self.product_suggestions_collapsed else "collapse"], width=36, height=32, corner_radius=7, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], command=self.toggle_product_suggestions)
        self.product_suggestions_toggle.grid(row=0,column=2,sticky="e",padx=(2,5),pady=4)
        self.m_quantity_entry = ctk.CTkEntry(add_row, textvariable=self.m_quantity, placeholder_text="Quantidade", width=120, height=38, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.m_quantity_entry.grid(row=0, column=1, padx=(0, 8))
        self.m_add_button = ctk.CTkButton(add_row, text="Adicionar", width=105, height=38, corner_radius=9, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.add_draft_item)
        self.m_add_button.grid(row=0, column=2)
        self.product_suggestions=ctk.CTkScrollableFrame(items,height=160,corner_radius=9,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"],scrollbar_button_color=COLORS["accent"],scrollbar_button_hover_color=COLORS["accent_hover"])
        self.draft_tree = self.table(items, ("product", "quantity"), ("Produto", "Quantidade"), (360, 120))
        self.draft_tree.configure(height=4)
        self.draft_tree.pack(fill="x")
        draft_actions = ctk.CTkFrame(items, fg_color="transparent")
        draft_actions.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(draft_actions, text="Remover", image=self.icons["trash"], width=95, height=34, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["danger"], command=self.remove_draft_item).pack(side="left")
        ctk.CTkButton(draft_actions, text="Editar item", image=self.icons["edit"], width=105, height=34, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.edit_draft_item).pack(side="left", padx=(6, 0))
        self.m_save_button = ctk.CTkButton(draft_actions, text="Salvar movimentação (0 itens)", height=40, corner_radius=9, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], state="disabled", command=self.register_movement)
        self.m_save_button.pack(side="right")

        history = Card(page)
        history.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(history, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=16)
        ctk.CTkLabel(bar, text="Histórico", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(side="left")
        self.history_filter = tk.StringVar(value=self.settings.get("history_filter","Todas as operações"))
        self.history_filter_menu = ctk.CTkOptionMenu(bar, variable=self.history_filter, values=["Todas as operações"], width=175, height=36, fg_color=COLORS["surface_alt"], button_color=COLORS["surface_hover"], text_color=COLORS["text"], command=lambda _value:(self.refresh_movements(),self.save_interface_state()))
        self.history_filter_menu.pack(side="right")
        ctk.CTkButton(bar, text="Excluir", image=self.icons["trash"], width=92, height=36, corner_radius=9, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["danger"], command=self.delete_movement).pack(side="right", padx=(8, 8))
        ctk.CTkButton(bar, text="Editar", image=self.icons["edit"], width=92, height=36, corner_radius=9, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.edit_movement).pack(side="right")
        self.history_tree = self.table(history, ("batch", "date", "operation", "product", "quantity", "stock", "user", "reason"), ("Lote", "Data", "Operação", "Produto", "Alteração", "Saldo", "Usuário", "Observação"), (60, 78, 115, 170, 82, 72, 105, 170))
        self.history_tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.history_tree.bind("<Double-1>", lambda _event: self.edit_movement())
        self.configure_tables()
        if not self.product_suggestions_collapsed:self.show_product_suggestions()
        return page

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
        return [product for product in self.db.products() if product_matches_search(product,query)]
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
            for product in results:
                label=self.movement_product_display(product)
                ctk.CTkButton(self.product_suggestions,text=label,anchor="w",height=34,corner_radius=6,fg_color="transparent",hover_color=COLORS["accent_soft"],text_color=COLORS["text"],command=lambda product_id=int(product["id"]):self.select_movement_product(product_id)).pack(fill="x",padx=5,pady=2)
        self.product_suggestions.pack(fill="x",pady=(0,10),before=self.draft_tree)
    def on_product_search(self,event=None):
        if event and event.keysym in ("Return","Escape"):
            if event.keysym=="Escape":self.hide_product_suggestions()
            return
        selected=self.db.product(self.m_selected_product_id) if self.m_selected_product_id else None
        if not selected or self.m_product.get()!=self.movement_product_display(selected):self.m_selected_product_id=None;self.update_current_stock()
        self.show_product_suggestions(force=True)
    def select_first_product_suggestion(self):
        results=self.movement_product_results(self.m_product.get())
        if results:self.select_movement_product(int(results[0]["id"]));self.m_quantity_entry.focus_set()
    def select_movement_product(self,product_id):
        product=self.db.product(int(product_id))
        if not product:return
        self.m_selected_product_id=int(product_id);self.m_product.set(self.movement_product_display(product));self.hide_product_suggestions();self.update_current_stock()
    def operation_map(self, include_inactive=False):return {str(operation["name"]):int(operation["id"]) for operation in self.db.operations(include_inactive=include_inactive)}
    def user_names(self, include_inactive=False):return [str(user["name"]) for user in self.db.users(include_inactive=include_inactive)]
    def refresh_user_controls(self):
        names=self.user_names();values=names or ["Cadastre um usuário na aba Cadastro"]
        if hasattr(self,"m_user_menu"):
            self.m_user_menu.configure(values=values)
            if self.m_user.get() not in names:self.m_user.set(names[0] if names else values[0])
        if hasattr(self,"c_responsible_menu"):
            self.c_responsible_menu.configure(values=values)
            if self.c_responsible.get() not in names:self.c_responsible.set(names[0] if names else values[0])
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
            all_operations = self.operation_map(include_inactive=True)
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
        count=len(self.movement_draft);self.m_save_button.configure(text=f"Salvar movimentação ({count} {'item' if count==1 else 'itens'})",state="normal" if count else "disabled")
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
        try:movement_date=self.m_date_entry.get_date();self.db.add_movement_batch(operation_id,[(item["product_id"],item["quantity"]) for item in self.movement_draft],movement_date.isoformat(),self.m_reason.get().strip(),self.m_user.get())
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        count=len(self.movement_draft);self.movement_draft.clear();self.draft_edit_index=None;self.m_quantity.set("");self.m_reason.set("");self.m_date_entry.set_date(date.today());self.refresh_draft();self.refresh_all();self.update_current_stock();self.show_movement_result(f"Movimentação registrada com {count} {'produto' if count==1 else 'produtos'}.")

    def show_movement_result(self, success_message: str):
        negative_products=self.db.negative_stock_products()
        if not negative_products:messagebox.showinfo(APP_NAME,success_message,parent=self);return
        details="\n".join(f"• {product_label(product)}: {fmt_number(product['stock'])} {product['unit']}" for product in negative_products)
        messagebox.showwarning(APP_NAME,f"{success_message}\n\nATENÇÃO: ESTOQUE NEGATIVO\n{details}\n\nA alteração foi concluída sem bloquear o saldo negativo. Verifique o ocorrido e registre uma entrada ou um ajuste positivo para corrigir o saldo.",parent=self)
    def selected_movement(self):
        selected=self.history_tree.selection();return int(selected[0]) if selected else None
    def edit_movement(self):
        movement_id=self.selected_movement()
        if not movement_id:messagebox.showinfo(APP_NAME,"Selecione uma movimentação para editar.",parent=self);return
        movement=self.db.movement(movement_id)
        if not movement:messagebox.showwarning(APP_NAME,"A movimentação não existe mais.",parent=self);self.refresh_movements();return
        dialog=MovementDialog(self,movement);self.wait_window(dialog)
        if not dialog.result:return
        try:self.db.update_movement(movement_id,**dialog.result)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.update_current_stock();self.show_movement_result("Movimentação atualizada.")
    def delete_movement(self):
        movement_id=self.selected_movement()
        if not movement_id:messagebox.showinfo(APP_NAME,"Selecione uma movimentação para excluir.",parent=self);return
        movement=self.db.movement(movement_id)
        if not movement:messagebox.showwarning(APP_NAME,"A movimentação não existe mais.",parent=self);self.refresh_movements();return
        movement_date=datetime.strptime(movement["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y")
        if not messagebox.askyesno(APP_NAME,f"Excluir a movimentação de {product_label(movement)} em {movement_date}?\n\nO saldo do produto será recalculado.",icon="warning",parent=self):return
        try:self.db.delete_movement(movement_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.update_current_stock();self.show_movement_result("Movimentação excluída.")
    def refresh_movements(self):
        if not hasattr(self,"history_tree"):return
        self.refresh_user_controls()
        if self.m_selected_product_id and not self.db.product(self.m_selected_product_id):self.m_selected_product_id=None;self.m_product.set("");self.update_current_stock()
        self.refresh_operation_controls();self.history_tree.delete(*self.history_tree.get_children())
        operation_filter=getattr(self,"history_operation_mapping",{}).get(self.history_filter.get(),"todos")
        for m in self.db.movements(operation_filter):
            qty=float(m["quantity"]);self.history_tree.insert("","end",iid=str(m["id"]),values=(f"#{m['batch_id']}" if m["batch_id"] else "—",datetime.strptime(m["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y"),m["operation_name"],product_label(m),f"{'+' if qty>0 else ''}{fmt_number(qty)} {m['unit']}",f"{fmt_number(m['resulting_stock'])} {m['unit']}",m["checked_by"]or"—",m["reason"]))

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
            connection=sqlite3.connect(self.db.path)
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
    def refresh_all(self):self.refresh_products();self.refresh_stock();self.refresh_movements();self.refresh_simulation();self.refresh_counts()
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
