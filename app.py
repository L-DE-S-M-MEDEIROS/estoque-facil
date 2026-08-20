from __future__ import annotations

import ctypes
import json
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import ImageTk

from premium_icons import app_icon, brand_mark, icon
from premium_widgets import ConfidenceGauge, MaskedDateEntry, confidence_tier

APP_NAME = "ESTOQUE BOLSAS BABY"
APP_VERSION = "0.8.0"
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


def product_label(product: sqlite3.Row) -> str:
    parts = [product["group_name"], product["name"], product["variant"]]
    return " • ".join(str(part) for part in parts if part)


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
            CREATE TABLE IF NOT EXISTS movements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('entrada','saida','ajuste','inventario')),
                quantity REAL NOT NULL, resulting_stock REAL NOT NULL, informed_quantity REAL,
                movement_date TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                checked_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT);
            CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id);
            CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(movement_date DESC);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(products)")}
        if "group_name" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        if "variant" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
        movement_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(movements)")}
        if "checked_by" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN checked_by TEXT NOT NULL DEFAULT ''")
        if "informed_quantity" not in movement_columns:
            self.db.execute("ALTER TABLE movements ADD COLUMN informed_quantity REAL")
            self.db.execute("UPDATE movements SET informed_quantity=resulting_stock WHERE type IN ('ajuste','inventario')")
        self.db.commit()

    def products(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id
            WHERE p.name LIKE ? OR p.category LIKE ? OR p.group_name LIKE ? OR p.variant LIKE ?
            GROUP BY p.id ORDER BY p.group_name COLLATE NOCASE,p.name COLLATE NOCASE,p.variant COLLATE NOCASE""", (term, term, term, term)).fetchall()

    def groups(self) -> list[str]:
        return [row["group_name"] for row in self.db.execute("SELECT DISTINCT group_name FROM products WHERE group_name<>'' ORDER BY group_name COLLATE NOCASE")]

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

    def stock(self, product_id: int) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(quantity),0) value FROM movements WHERE product_id=?", (product_id,)).fetchone()["value"])

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
            if balance < -.0000001:
                raise ValueError(f"A alteração deixaria o estoque negativo em {datetime.strptime(row['movement_date'], '%Y-%m-%d').strftime('%d/%m/%y')}.")
            self.db.execute("UPDATE movements SET quantity=?,resulting_stock=? WHERE id=?", (quantity, balance, row["id"]))

    def add_movement(self, product_id: int, kind: str, informed: float, movement_date: str, reason: str, checked_by: str = "") -> None:
        created_at = datetime.now().isoformat(timespec="microseconds")
        balance_before = self._balance_before(product_id, movement_date, created_at, 2**63-1)
        delta = self._movement_delta(kind, informed, balance_before)
        informed_quantity = informed if kind in ("ajuste", "inventario") else None
        with self.db:
            self.db.execute("INSERT INTO movements(product_id,type,quantity,resulting_stock,informed_quantity,movement_date,reason,checked_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (product_id, kind, delta, balance_before + delta, informed_quantity, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), checked_by.strip(), created_at))
            self._recalculate_product(product_id)

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
        return self.db.execute("""SELECT m.*,p.name,p.group_name,p.variant,p.unit FROM movements m
            JOIN products p ON p.id=m.product_id WHERE m.id=?""", (movement_id,)).fetchone()

    def update_movement(self, movement_id: int, product_id: int, kind: str, informed: float, movement_date: str, reason: str) -> None:
        previous = self.movement(movement_id)
        if not previous:
            raise ValueError("A movimentação não existe mais.")
        balance_before = self._balance_before(product_id, movement_date, previous["created_at"], movement_id)
        delta = self._movement_delta(kind, informed, balance_before)
        informed_quantity = informed if kind in ("ajuste", "inventario") else None
        affected = {int(previous["product_id"]), product_id}
        with self.db:
            self.db.execute("""UPDATE movements SET product_id=?,type=?,quantity=?,informed_quantity=?,movement_date=?,reason=?
                WHERE id=?""", (product_id, kind, delta, informed_quantity, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), movement_id))
            for affected_product in affected:
                self._recalculate_product(affected_product)

    def delete_movement(self, movement_id: int) -> None:
        movement = self.movement(movement_id)
        if not movement:
            raise ValueError("A movimentação não existe mais.")
        with self.db:
            self.db.execute("DELETE FROM movements WHERE id=?", (movement_id,))
            self._recalculate_product(int(movement["product_id"]))

    def movements(self, kind: str = "todos") -> list[sqlite3.Row]:
        where, args = ("", ()) if kind == "todos" else ("WHERE m.type=?", (kind,))
        return self.db.execute(f"""SELECT m.*,p.name,p.group_name,p.variant,p.unit FROM movements m JOIN products p ON p.id=m.product_id
            {where} ORDER BY m.movement_date DESC,m.created_at DESC LIMIT 500""", args).fetchall()

    def backup(self, target: Path) -> None:
        self.db.commit(); shutil.copy2(self.path, target)

    def restore(self, source: Path) -> None:
        self.db.close(); shutil.copy2(source, self.path); self.__init__()

    def clear(self) -> None:
        self.db.execute("DELETE FROM movements"); self.db.execute("DELETE FROM products"); self.db.commit()


class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["surface"], corner_radius=12, border_width=1, border_color=COLORS["border"], **kwargs)


class PageTitle(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str):
        super().__init__(master, fg_color="transparent")
        ctk.CTkLabel(self, text=title, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 25, "bold")).pack(anchor="w")
        ctk.CTkLabel(self, text=subtitle, text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 12)).pack(anchor="w", pady=(5, 0))


class ProductDialog(ctk.CTkToplevel):
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
        self.group_name = self.field(form, "Grupo / modelo", 2, 1, product["group_name"] if product else "")
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
        self.result = {"name": name, "category": self.category.get().strip(), "group_name": self.group_name.get().strip(), "variant": self.variant.get().strip(), "unit": self.unit.get(), "minimum": minimum, "photo": photo, "notes": self.notes.get("1.0", "end").strip()}; self.destroy()


class MovementDialog(ctk.CTkToplevel):
    def __init__(self, parent: "EstoqueApp", movement: sqlite3.Row):
        super().__init__(parent, fg_color=COLORS["background"])
        self.parent, self.movement, self.result = parent, movement, None
        self.product_mapping = parent.product_map()
        self.title("Editar movimentação")
        scale = parent.ui_scale
        width, height = round(520 * scale), round(610 * scale)
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

        self.kind = tk.StringVar(value=movement["type"])
        label("Operação")
        ctk.CTkOptionMenu(form, variable=self.kind, values=["entrada", "saida", "ajuste", "inventario"], height=40, corner_radius=9, fg_color=COLORS["surface"], button_color=COLORS["surface_alt"], button_hover_color=COLORS["surface_hover"], text_color=COLORS["text"], dropdown_fg_color=COLORS["surface"], dropdown_hover_color=COLORS["accent_soft"]).pack(fill="x", pady=(0, 16))

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
        self.result = {"product_id": product_id, "kind": self.kind.get(), "informed": informed, "movement_date": movement_date, "reason": self.reason.get().strip()}
        self.destroy()


class EstoqueApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=COLORS["background"])
        self.withdraw()
        dpi = float(self.winfo_fpixels("1i")); self.ui_scale = max(1, min(dpi/96, 3)); self.tk.call("tk", "scaling", dpi/72)
        self.settings_path = data_dir()/"settings.json"; self.settings = self.load_settings(); ctk.set_appearance_mode(self.settings.get("theme", "Light"))
        self.db = Database(); self.title(f"{APP_NAME} — v{APP_VERSION}")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight(); self.work_areas = monitor_work_areas(sw, sh)
        primary = self.work_areas[0]; work_width, work_height = primary[2]-primary[0], primary[3]-primary[1]
        self.minimum_width = min(work_width, round(1050*self.ui_scale)); self.minimum_height = min(work_height, round(680*self.ui_scale))
        self.minsize(self.minimum_width, self.minimum_height)
        self._normal_geometry = visible_window_geometry(self.settings.get("window_geometry"), self.work_areas, self.minimum_width, self.minimum_height)
        self.geometry(self._normal_geometry)
        self._last_window_state = self.settings.get("window_state", "zoomed") if self.settings.get("window_state") in ("normal", "zoomed") else "zoomed"
        self.iconphoto(True, ImageTk.PhotoImage(app_icon(256))); self.protocol("WM_DELETE_WINDOW", self.close)
        self.brand_icon = brand_mark(86)
        self.icons = {name: icon(name, 22) for name in ("products", "stock", "movements", "count", "settings", "plus", "search", "edit", "trash", "download", "upload", "refresh")}
        self.nav_buttons = {}; self.pages = {}; self.build_shell(); self.show_page("stock")
        self.bind("<Configure>", self.remember_window_geometry)
        self.after_idle(self.restore_window)

    def load_settings(self):
        try: return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {"theme": "Light"}

    def save_settings(self): self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

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
        for key, label in (("stock","Estoque atual"),("movements","Movimentações"),("count","Contagem"),("products","Produtos"),("settings","Configurações")):
            button = ctk.CTkButton(self.sidebar, text=label, image=self.icons[key], compound="left", anchor="w", height=48, corner_radius=10, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 13, "bold"), command=lambda k=key:self.show_page(k))
            button.pack(fill="x", padx=16, pady=4); self.nav_buttons[key]=button
        ctk.CTkLabel(self.sidebar, text=f"●  Dados locais protegidos\n    Versão {APP_VERSION}", justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(side="bottom", anchor="w", padx=26, pady=28)
        self.content = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0); self.content.grid(row=0,column=1,sticky="nsew"); self.content.grid_columnconfigure(0,weight=1); self.content.grid_rowconfigure(0,weight=1)

    def show_page(self,key):
        for page in self.pages.values(): page.grid_remove()
        if key not in self.pages: self.pages[key]={"products":self.products_page,"stock":self.stock_page,"movements":self.movements_page,"count":self.count_page,"settings":self.settings_page}[key]()
        self.pages[key].grid(row=0,column=0,sticky="nsew",padx=32,pady=28)
        for name,button in self.nav_buttons.items():
            selected = name == key
            button.configure(
                fg_color=COLORS["nav_selected"] if selected else "transparent",
                text_color=COLORS["accent"] if selected else COLORS["muted"],
                border_width=1 if selected else 0,
                border_color=COLORS["accent"] if selected else COLORS["sidebar"],
            )
        {"products":self.refresh_products,"stock":self.refresh_stock,"movements":self.refresh_movements,"count":self.refresh_counts,"settings":lambda:None}[key]()

    def table(self,parent,columns,headings,widths):
        tree=ttk.Treeview(parent,columns=columns,show="headings",selectmode="browse")
        for col,label,width in zip(columns,headings,widths): tree.heading(col,text=label); tree.column(col,width=width,anchor="e" if col in ("minimum","stock","quantity","difference") else "w")
        return tree

    def configure_tables(self):
        dark=ctk.get_appearance_mode()=="Dark"; bg="#121824" if dark else "#FFFFFF"; fg="#F3F7FB" if dark else "#202936"; head="#192232" if dark else "#EEF3F8"; selected="#203C52" if dark else "#DDEFFC"
        style=ttk.Style(self); style.theme_use("clam"); style.configure("Treeview",background=bg,fieldbackground=bg,foreground=fg,rowheight=max(38,round(34*self.ui_scale)),borderwidth=0,font=("Inter",10)); style.configure("Treeview.Heading",background=head,foreground=fg,relief="flat",font=("Inter",9,"bold"),padding=10); style.map("Treeview",background=[("selected",selected)],foreground=[("selected",fg)])

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
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Estoque atual","Uma visão clara dos saldos e itens que precisam de atenção.").pack(fill="x",pady=(0,22));cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,16));self.stock_cards=[]
        for title in ("Produtos","Unidades em estoque","Abaixo do mínimo","Confiança baixa"):
            card=Card(cards,height=108);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=18,pady=(17,3));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold"));label.pack(anchor="w",padx=18);self.stock_cards.append(label)
        card=Card(page);card.pack(fill="both",expand=True);bar=ctk.CTkFrame(card,fg_color="transparent");bar.pack(fill="x",padx=20,pady=(18,8));ctk.CTkLabel(bar,text="Posição do estoque",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(side="left");self.stock_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto, grupo ou variação...",width=300,height=38,corner_radius=9);self.stock_search.pack(side="right");self.stock_search.bind("<KeyRelease>",lambda e:self.refresh_stock())
        self.stock_tree=self.table(card,("group","name","variant","stock","unit","minimum","status","confidence"),("Grupo / modelo","Produto","Variação","Saldo atual","Un.","Mínimo","Situação","Confiança"),(145,160,125,80,45,60,100,105));self.stock_tree.pack(fill="both",expand=True,padx=20,pady=(8,20));self.configure_tables();return page

    def refresh_stock(self):
        if not hasattr(self,"stock_tree"):return
        items=self.db.products(self.stock_search.get() if hasattr(self,"stock_search") else "");self.stock_tree.delete(*self.stock_tree.get_children());units=low=low_confidence=0
        for p in items:
            stock=float(p["stock"]);units+=stock;status="Sem estoque" if stock<=0 else "Estoque baixo" if stock<=float(p["minimum"]) else "Normal";low+=status!="Normal";trust=self.db.stock_confidence(int(p["id"]),stock);low_confidence+=trust["level"]=="Baixa";self.stock_tree.insert("","end",iid=str(p["id"]),values=(p["group_name"]or"Sem grupo",p["name"],p["variant"]or"—",fmt_number(stock),p["unit"],fmt_number(p["minimum"]),status,f"{trust['score']}% • {trust['level']}"))
        for label,text in zip(self.stock_cards,(str(len(items)),fmt_number(units),str(low),str(low_confidence))):label.configure(text=text)

    def count_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Contagem","Faça o check-in físico do estoque e recupere a confiança dos saldos.").pack(fill="x",pady=(0,18))
        ctk.CTkLabel(page,text="A confiança diminui conforme passam os dias e aumentam a quantidade e a frequência das movimentações desde a última contagem.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),anchor="w").pack(fill="x",pady=(0,12))
        cards=ctk.CTkFrame(page,fg_color="transparent");cards.pack(fill="x",pady=(0,16));self.count_cards=[]
        for title in ("A conferir","Conferidos hoje","Diferenças hoje","Confiança média"):
            card=Card(cards,height=92);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=16,pady=(13,2));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",20,"bold"));label.pack(anchor="w",padx=16);self.count_cards.append(label)
        body=ctk.CTkFrame(page,fg_color="transparent");body.pack(fill="both",expand=True);body.grid_columnconfigure(1,weight=1);body.grid_rowconfigure(0,weight=1)
        form=Card(body,width=330);form.grid(row=0,column=0,sticky="ns",padx=(0,16));form.grid_propagate(False);ctk.CTkLabel(form,text="Novo check-in",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(anchor="w",padx=20,pady=(16,10));self.c_product=tk.StringVar();self.c_quantity=tk.StringVar();self.c_responsible=tk.StringVar(value=self.settings.get("counter_name",""));self.c_note=tk.StringVar()
        def count_label(text):ctk.CTkLabel(form,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=20,pady=(0,4))
        count_label("Produto")
        self.c_product_combo=ctk.CTkOptionMenu(form,variable=self.c_product,values=[""],height=36,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"],command=lambda _v:self.update_count_current());self.c_product_combo.pack(fill="x",padx=20,pady=(0,8))
        self.count_current=ctk.CTkLabel(form,text="Saldo do sistema: —",height=32,corner_radius=9,fg_color=COLORS["accent_soft"],text_color=COLORS["accent"],font=ctk.CTkFont("Inter",10,"bold"));self.count_current.pack(fill="x",padx=20,pady=(0,8))
        count_label("Quantidade física contada")
        self.c_quantity_entry=ctk.CTkEntry(form,textvariable=self.c_quantity,height=36,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.c_quantity_entry.pack(fill="x",padx=20,pady=(0,8))
        count_label("Data (DD/MM/AA)")
        self.c_date_entry=MaskedDateEntry(form,COLORS,initial=date.today(),control_height=36);self.c_date_entry.pack(fill="x",padx=20,pady=(0,8))
        count_label("Responsável pela contagem")
        ctk.CTkEntry(form,textvariable=self.c_responsible,height=36,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"],placeholder_text="Nome ou código").pack(fill="x",padx=20,pady=(0,8))
        count_label("Observação opcional")
        ctk.CTkEntry(form,textvariable=self.c_note,height=36,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]).pack(fill="x",padx=20,pady=(0,10))
        ctk.CTkButton(form,text="Confirmar contagem",height=40,corner_radius=10,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.register_count).pack(fill="x",padx=20,pady=(0,14))
        listing=Card(body);listing.grid(row=0,column=1,sticky="nsew");bar=ctk.CTkFrame(listing,fg_color="transparent");bar.pack(fill="x",padx=20,pady=16);ctk.CTkLabel(bar,text="Check-in dos produtos",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left")
        self.count_filter=tk.StringVar(value="todos");ctk.CTkOptionMenu(bar,variable=self.count_filter,values=["todos","pendentes","verificados"],width=110,height=36,fg_color=COLORS["surface_alt"],button_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda _v:self.refresh_counts()).pack(side="right")
        ctk.CTkButton(bar,text="Contar",image=self.icons["count"],width=95,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.prepare_count).pack(side="right",padx=(0,8))
        ctk.CTkButton(bar,text="Explicar",width=90,height=36,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.explain_confidence).pack(side="right",padx=(0,8))
        self.count_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produto...",width=165,height=36,corner_radius=9);self.count_search.pack(side="right",padx=(0,8));self.count_search.bind("<KeyRelease>",lambda _e:self.refresh_counts())
        confidence_panel=ctk.CTkFrame(listing,height=174,corner_radius=10,fg_color=COLORS["surface_alt"],border_width=1,border_color=COLORS["border"]);confidence_panel.pack(fill="x",padx=20,pady=(0,14));confidence_panel.pack_propagate(False)
        self.confidence_gauge=ConfidenceGauge(confidence_panel,COLORS,width=248);self.confidence_gauge.pack(side="left",padx=(16,20),pady=10)
        confidence_text=ctk.CTkFrame(confidence_panel,fg_color="transparent");confidence_text.pack(side="left",fill="both",expand=True,pady=22,padx=(0,18))
        self.confidence_title=ctk.CTkLabel(confidence_text,text="Confiança média do estoque",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold"),anchor="w");self.confidence_title.pack(fill="x")
        self.confidence_description=ctk.CTkLabel(confidence_text,text="Visão geral calculada a partir de todos os produtos cadastrados.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10),justify="left",anchor="nw",wraplength=460);self.confidence_description.pack(fill="x",pady=(7,9))
        legend=ctk.CTkFrame(confidence_text,fg_color="transparent");legend.pack(fill="x")
        for text,color in (("● Baixa",("#C94B4B","#FF6B6B")),("● Média",("#B77912","#FFD166")),("● Alta",("#27845E","#4ADE80")),("● Máxima",("#2478C4","#38BDF8"))):ctk.CTkLabel(legend,text=text,text_color=color,font=ctk.CTkFont("Inter",9,"bold")).pack(side="left",padx=(0,14))
        self.count_tree=self.table(listing,("product","stock","checkin","date","responsible","confidence","difference"),("Produto","Estoque atual","Check-in","Data","Responsável","Confiança","Diferença"),(170,75,75,70,80,85,85));self.count_tree.pack(fill="both",expand=True,padx=20,pady=(0,20));self.count_tree.bind("<<TreeviewSelect>>",lambda _e:self.update_confidence_gauge());self.count_tree.bind("<Double-1>",lambda _e:self.prepare_count());self.configure_tables();return page

    def update_count_current(self):
        pid=self.product_map().get(self.c_product.get()) if hasattr(self,"c_product") else None
        if not hasattr(self,"count_current"):return
        product=self.db.product(pid) if pid else None
        self.count_current.configure(text=f"Saldo do sistema: {fmt_number(product['stock'])} {product['unit']}" if product else "Saldo do sistema: —")
        if product:self.show_product_confidence(product)

    def show_product_confidence(self,product):
        if not hasattr(self,"confidence_gauge"):return
        trust=self.db.stock_confidence(int(product["id"]),float(product["stock"]));self.confidence_gauge.set_score(trust["score"]);self.confidence_title.configure(text=product_label(product));self.confidence_description.configure(text=f"{trust['level']} • {trust['days']} dia(s) desde a referência • {trust['movement_count']} movimentação(ões) • {fmt_number(trust['moved_units'])} {product['unit']} movimentadas.")

    def update_confidence_gauge(self):
        pid=self.selected_count_product()
        if pid:self.show_product_confidence(self.db.product(pid))

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
        if not responsible:messagebox.showwarning(APP_NAME,"Informe quem realizou a contagem.",parent=self);return
        try:amount=float(self.c_quantity.get().replace(",","."));count_date=self.c_date_entry.get_date()
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        if amount<0:messagebox.showwarning(APP_NAME,"A quantidade contada não pode ser negativa.",parent=self);return
        previous=self.db.stock(pid);difference=amount-previous
        try:self.db.add_movement(pid,"inventario",amount,count_date.isoformat(),self.c_note.get().strip() or "Contagem física",checked_by=responsible)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.settings["counter_name"]=responsible;self.save_settings();self.c_quantity.set("");self.c_note.set("");self.c_date_entry.set_date(date.today());self.refresh_all();self.update_count_current();messagebox.showinfo(APP_NAME,f"Contagem confirmada.\nDiferença encontrada: {'+' if difference>0 else ''}{fmt_number(difference)}",parent=self)

    def refresh_counts(self):
        if not hasattr(self,"count_tree"):return
        mapping=self.product_map();self.c_product_combo.configure(values=list(mapping)or[""]);search=self.count_search.get() if hasattr(self,"count_search") else "";items=self.db.products(search);self.count_tree.delete(*self.count_tree.get_children());all_items=self.db.products();pending=counted_today=differences_today=total_score=0;today=date.today().isoformat();infos={}
        for p in all_items:
            trust=self.db.stock_confidence(int(p["id"]),float(p["stock"]));infos[int(p["id"])]=trust;pending+=trust["checkin"]=="PENDENTE";counted_today+=trust["last_date"]==today;differences_today+=trust["last_date"]==today and trust["last_difference"] is not None and abs(trust["last_difference"])>.0000001;total_score+=trust["score"]
        selected_filter=self.count_filter.get() if hasattr(self,"count_filter") else "todos"
        for p in items:
            trust=infos[int(p["id"])];
            if selected_filter=="pendentes" and trust["checkin"]!="PENDENTE":continue
            if selected_filter=="verificados" and trust["checkin"]!="VERIFICADO":continue
            last=datetime.strptime(trust["last_date"],"%Y-%m-%d").strftime("%d/%m/%y") if trust["last_date"] else "—";difference="—" if trust["last_difference"] is None else f"{'+' if trust['last_difference']>0 else ''}{fmt_number(trust['last_difference'])} {p['unit']}";self.count_tree.insert("","end",iid=str(p["id"]),values=(product_label(p),f"{fmt_number(p['stock'])} {p['unit']}",trust["checkin"],last,trust["checked_by"]or"—",f"{trust['score']}% • {trust['level']}",difference))
        average=round(total_score/len(all_items)) if all_items else 0
        for label,text in zip(self.count_cards,(str(pending),str(counted_today),str(differences_today),f"{average}%")):label.configure(text=text)
        if hasattr(self,"confidence_gauge") and not self.count_tree.selection():
            self.confidence_gauge.set_score(average if all_items else None);self.confidence_title.configure(text="Confiança média do estoque");self.confidence_description.configure(text="Visão geral calculada a partir de todos os produtos cadastrados." if all_items else "Cadastre um produto para começar a medir a confiança do estoque.")

    def movements_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Movimentações","Registre entradas, saídas, ajustes e contagens de inventário.").pack(fill="x",pady=(0,22));body=ctk.CTkFrame(page,fg_color="transparent");body.pack(fill="both",expand=True);body.grid_columnconfigure(1,weight=1);body.grid_rowconfigure(0,weight=1)
        form=Card(body,width=350);form.grid(row=0,column=0,sticky="ns",padx=(0,16));form.grid_propagate(False);ctk.CTkLabel(form,text="Nova movimentação",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(anchor="w",padx=20,pady=(22,18));self.m_type=tk.StringVar(value="entrada");self.m_product=tk.StringVar();self.m_quantity=tk.StringVar();self.m_reason=tk.StringVar()
        def field_label(text): ctk.CTkLabel(form,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10,"bold")).pack(anchor="w",padx=20,pady=(0,6))
        field_label("Operação")
        self.m_type_menu=ctk.CTkOptionMenu(form,variable=self.m_type,values=["entrada","saida","ajuste","inventario"],height=40,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"])
        self.m_type_menu.pack(fill="x",padx=20,pady=(0,14))
        field_label("Produto")
        self.m_product_combo=ctk.CTkOptionMenu(form,variable=self.m_product,values=[""],height=40,corner_radius=9,fg_color=COLORS["surface"],button_color=COLORS["surface_alt"],button_hover_color=COLORS["surface_hover"],text_color=COLORS["text"],dropdown_fg_color=COLORS["surface"],dropdown_hover_color=COLORS["accent_soft"],command=lambda _v:self.update_current_stock())
        self.m_product_combo.pack(fill="x",padx=20,pady=(0,14))
        field_label("Quantidade / nova contagem")
        ctk.CTkEntry(form,textvariable=self.m_quantity,height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]).pack(fill="x",padx=20,pady=(0,14))
        field_label("Data (DD/MM/AA)")
        self.m_date_entry=MaskedDateEntry(form,COLORS,initial=date.today())
        self.m_date_entry.pack(fill="x",padx=20,pady=(0,14))
        field_label("Motivo ou observação")
        ctk.CTkEntry(form,textvariable=self.m_reason,height=40,corner_radius=9,border_color=COLORS["border"],fg_color=COLORS["surface"]).pack(fill="x",padx=20,pady=(0,14))
        self.current_stock=ctk.CTkLabel(form,text="Saldo atual: —",height=40,corner_radius=9,fg_color=COLORS["accent_soft"],text_color=COLORS["accent"],font=ctk.CTkFont("Inter",11,"bold"));self.current_stock.pack(fill="x",padx=20,pady=(0,16));ctk.CTkButton(form,text="Registrar movimentação",height=44,corner_radius=10,fg_color=COLORS["accent"],hover_color=COLORS["accent_hover"],command=self.register_movement).pack(fill="x",padx=20,pady=(0,22))
        history=Card(body);history.grid(row=0,column=1,sticky="nsew");bar=ctk.CTkFrame(history,fg_color="transparent");bar.pack(fill="x",padx=20,pady=18);ctk.CTkLabel(bar,text="Histórico",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left")
        self.history_filter=tk.StringVar(value="todos")
        ctk.CTkOptionMenu(bar,variable=self.history_filter,values=["todos","entrada","saida","ajuste","inventario"],width=150,height=38,fg_color=COLORS["surface_alt"],button_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda _v:self.refresh_movements()).pack(side="right")
        ctk.CTkButton(bar,text="Excluir",image=self.icons["trash"],width=92,height=38,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"],command=self.delete_movement).pack(side="right",padx=(8,8))
        ctk.CTkButton(bar,text="Editar",image=self.icons["edit"],width=92,height=38,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["text"],command=self.edit_movement).pack(side="right")
        self.history_tree=self.table(history,("date","product","type","quantity","stock","reason"),("Data","Produto","Operação","Alteração","Saldo","Observação"),(90,170,95,90,80,230));self.history_tree.pack(fill="both",expand=True,padx=20,pady=(0,20));self.history_tree.bind("<Double-1>",lambda _event:self.edit_movement());self.configure_tables();return page

    def product_map(self):return {f"{product_label(p)}  [{p['unit']}]":int(p["id"]) for p in self.db.products()}
    def update_current_stock(self):
        pid=self.product_map().get(self.m_product.get());self.current_stock.configure(text=f"Saldo atual: {fmt_number(self.db.stock(pid))}" if pid else "Saldo atual: —")
    def register_movement(self):
        pid=self.product_map().get(self.m_product.get())
        if not pid:messagebox.showwarning(APP_NAME,"Selecione um produto.",parent=self);return
        try:amount=float(self.m_quantity.get().replace(",","."));movement_date=self.m_date_entry.get_date();self.db.add_movement(pid,self.m_type.get(),amount,movement_date.isoformat(),self.m_reason.get().strip())
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        self.m_quantity.set("");self.m_reason.set("");self.m_date_entry.set_date(date.today());self.refresh_all();self.update_current_stock();messagebox.showinfo(APP_NAME,"Movimentação registrada.",parent=self)
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
        self.refresh_all();self.update_current_stock();messagebox.showinfo(APP_NAME,"Movimentação atualizada.",parent=self)
    def delete_movement(self):
        movement_id=self.selected_movement()
        if not movement_id:messagebox.showinfo(APP_NAME,"Selecione uma movimentação para excluir.",parent=self);return
        movement=self.db.movement(movement_id)
        if not movement:messagebox.showwarning(APP_NAME,"A movimentação não existe mais.",parent=self);self.refresh_movements();return
        movement_date=datetime.strptime(movement["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y")
        if not messagebox.askyesno(APP_NAME,f"Excluir a movimentação de {product_label(movement)} em {movement_date}?\n\nO saldo do produto será recalculado.",icon="warning",parent=self):return
        try:self.db.delete_movement(movement_id)
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error),parent=self);return
        self.refresh_all();self.update_current_stock();messagebox.showinfo(APP_NAME,"Movimentação excluída.",parent=self)
    def refresh_movements(self):
        if not hasattr(self,"history_tree"):return
        mapping=self.product_map();self.m_product_combo.configure(values=list(mapping)or[""]);self.history_tree.delete(*self.history_tree.get_children());labels={"entrada":"Entrada","saida":"Saída","ajuste":"Ajuste","inventario":"Inventário"}
        for m in self.db.movements(self.history_filter.get()):qty=float(m["quantity"]);self.history_tree.insert("","end",iid=str(m["id"]),values=(datetime.strptime(m["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y"),product_label(m),labels[m["type"]],f"{'+' if qty>0 else ''}{fmt_number(qty)} {m['unit']}",f"{fmt_number(m['resulting_stock'])} {m['unit']}",m["reason"]))

    def settings_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Configurações","Personalize a aparência e proteja seus dados.").pack(fill="x",pady=(0,22))
        appearance=Card(page);appearance.pack(fill="x",pady=(0,16));row=ctk.CTkFrame(appearance,fg_color="transparent");row.pack(fill="x",padx=22,pady=20);ctk.CTkLabel(row,text="Tema da interface",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w");ctk.CTkLabel(row,text="Escolha entre o modo claro off-white e o modo escuro em grafite.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",pady=(4,12));self.theme_selector=ctk.CTkSegmentedButton(row,values=["Light","Dark"],command=self.change_theme,selected_color=COLORS["accent"],selected_hover_color=COLORS["accent_hover"]);self.theme_selector.pack(anchor="w");self.theme_selector.set(self.settings.get("theme","Light"))
        actions=ctk.CTkFrame(page,fg_color="transparent");actions.pack(fill="both",expand=True);actions.grid_columnconfigure((0,1),weight=1)
        for index,(title,text,icon_name,command,button) in enumerate((("Atualizações",f"Versão instalada: {APP_VERSION}.","refresh",self.check_updates,"Buscar atualização"),("Backup dos dados","Salve uma cópia segura do banco local.","download",self.backup,"Baixar backup"),("Restaurar backup","Substitua os dados por um backup anterior.","upload",self.restore,"Restaurar backup"),("Apagar dados","Remove produtos e movimentações definitivamente.","trash",self.clear_data,"Apagar dados"))):
            card=Card(actions);card.grid(row=index//2,column=index%2,sticky="nsew",padx=(0 if index%2==0 else 8,8 if index%2==0 else 0),pady=8);ctk.CTkLabel(card,text=title,image=self.icons[icon_name],compound="left",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(anchor="w",padx=20,pady=(20,5));ctk.CTkLabel(card,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=20);ctk.CTkButton(card,text=button,height=38,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"] if title=="Apagar dados" else COLORS["text"],command=command).pack(anchor="w",padx=20,pady=20)
        return page

    def change_theme(self,value):
        self.settings["theme"]=value;self.save_settings();ctk.set_appearance_mode(value);self.configure_tables()
        if hasattr(self,"confidence_gauge"):self.confidence_gauge.redraw()
    def check_updates(self):
        try:
            req=urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",headers={"User-Agent":APP_NAME});release=json.load(urllib.request.urlopen(req,timeout=10));latest=release["tag_name"].lstrip("v")
            if latest==APP_VERSION:messagebox.showinfo(APP_NAME,"Você já usa a versão mais recente.",parent=self)
            elif messagebox.askyesno(APP_NAME,f"Versão {latest} disponível. Abrir para baixar?",parent=self):webbrowser.open(release["html_url"])
        except (urllib.error.URLError,KeyError,TimeoutError):messagebox.showerror(APP_NAME,"Não foi possível consultar o GitHub agora.",parent=self)
    def backup(self):
        target=filedialog.asksaveasfilename(parent=self,defaultextension=".db",initialfile=f"estoque-backup-{date.today()}.db",filetypes=[("Backup","*.db")]);
        if target:self.db.backup(Path(target));messagebox.showinfo(APP_NAME,"Backup salvo.",parent=self)
    def restore(self):
        source=filedialog.askopenfilename(parent=self,filetypes=[("Backup","*.db")]);
        if source and messagebox.askyesno(APP_NAME,"Substituir os dados atuais?",parent=self):self.db.restore(Path(source));self.refresh_all()
    def clear_data(self):
        if messagebox.askyesno(APP_NAME,"Apagar definitivamente todos os dados?",icon="warning",parent=self):self.db.clear();self.refresh_all()
    def refresh_all(self):self.refresh_products();self.refresh_stock();self.refresh_movements();self.refresh_counts()
    def close(self):
        try:
            state = self.state()
            if state in ("normal", "zoomed"): self._last_window_state = state
            if state == "normal" and parse_window_geometry(self.geometry()): self._normal_geometry = self.geometry()
            self.settings["window_state"] = self._last_window_state
            self.settings["window_geometry"] = visible_window_geometry(self._normal_geometry, self.work_areas, self.minimum_width, self.minimum_height)
            self.save_settings()
        finally:
            self.db.db.close(); self.destroy()


if __name__=="__main__":
    try:enable_dpi_awareness();EstoqueApp().mainloop()
    except Exception as error:(data_dir()/"erro.log").write_text(f"{datetime.now().isoformat()}\n{error!r}\n",encoding="utf-8");raise
