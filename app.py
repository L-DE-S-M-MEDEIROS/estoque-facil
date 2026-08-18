from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import ctypes
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

APP_NAME = "Estoque Fácil"
APP_VERSION = "0.3.0"
GITHUB_REPO = "L-DE-S-M-MEDEIROS/estoque-facil"


def enable_windows_dpi_awareness() -> None:
    """Use the monitor's real pixel density instead of Windows bitmap scaling."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def app_data_dir() -> Path:
    root = Path.home() / "AppData" / "Local" / "EstoqueFacil"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fotos").mkdir(exist_ok=True)
    return root


class Database:
    def __init__(self) -> None:
        self.path = app_data_dir() / "estoque.db"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT 'un',
                cost REAL,
                minimum REAL NOT NULL DEFAULT 0,
                photo TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('entrada','saida','ajuste','inventario')),
                quantity REAL NOT NULL,
                resulting_stock REAL NOT NULL,
                movement_date TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id);
            CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(movement_date DESC);
            """
        )
        self.db.commit()

    def products(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.db.execute(
            """SELECT p.*, COALESCE(SUM(m.quantity), 0) AS stock
               FROM products p LEFT JOIN movements m ON m.product_id=p.id
               WHERE p.name LIKE ? OR p.category LIKE ?
               GROUP BY p.id ORDER BY p.name COLLATE NOCASE""",
            (term, term),
        ).fetchall()

    def product(self, product_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT p.*, COALESCE(SUM(m.quantity), 0) AS stock
               FROM products p LEFT JOIN movements m ON m.product_id=p.id
               WHERE p.id=? GROUP BY p.id""",
            (product_id,),
        ).fetchone()

    def save_product(self, values: dict, product_id: int | None = None) -> None:
        fields = (values["name"], values["category"], values["unit"], values["cost"], values["minimum"], values["photo"], values["notes"])
        if product_id:
            self.db.execute("UPDATE products SET name=?,category=?,unit=?,cost=?,minimum=?,photo=?,notes=? WHERE id=?", fields + (product_id,))
        else:
            self.db.execute("INSERT INTO products(name,category,unit,cost,minimum,photo,notes,created_at) VALUES(?,?,?,?,?,?,?,?)", fields + (datetime.now().isoformat(timespec="seconds"),))
        self.db.commit()

    def delete_product(self, product_id: int) -> bool:
        used = self.db.execute("SELECT 1 FROM movements WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
        if used:
            return False
        self.db.execute("DELETE FROM products WHERE id=?", (product_id,))
        self.db.commit()
        return True

    def stock(self, product_id: int) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(quantity),0) AS stock FROM movements WHERE product_id=?", (product_id,)).fetchone()
        return float(row["stock"])

    def add_movement(self, product_id: int, kind: str, informed: float, movement_date: str, reason: str) -> None:
        current = self.stock(product_id)
        delta = -informed if kind == "saida" else informed
        if kind in ("ajuste", "inventario"):
            delta = informed - current
        if current + delta < 0:
            raise ValueError("A saída é maior que o saldo disponível.")
        if abs(delta) < 0.0000001:
            raise ValueError("A quantidade informada já é o saldo atual.")
        self.db.execute(
            "INSERT INTO movements(product_id,type,quantity,resulting_stock,movement_date,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (product_id, kind, delta, current + delta, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def movements(self, kind: str = "todos") -> list[sqlite3.Row]:
        where, params = ("", ()) if kind == "todos" else ("WHERE m.type=?", (kind,))
        return self.db.execute(
            f"""SELECT m.*, p.name, p.unit FROM movements m JOIN products p ON p.id=m.product_id
                 {where} ORDER BY m.movement_date DESC, m.created_at DESC LIMIT 500""",
            params,
        ).fetchall()

    def backup(self, target: Path) -> None:
        self.db.commit()
        shutil.copy2(self.path, target)

    def restore(self, source: Path) -> None:
        self.db.close()
        shutil.copy2(source, self.path)
        self.__init__()

    def clear(self) -> None:
        self.db.execute("DELETE FROM movements")
        self.db.execute("DELETE FROM products")
        self.db.commit()


class ProductDialog(tk.Toplevel):
    def __init__(self, parent: "EstoqueApp", product: sqlite3.Row | None = None) -> None:
        super().__init__(parent)
        self.parent = parent
        self.product = product
        self.result: dict | None = None
        self.photo_path = tk.StringVar(value=product["photo"] if product else "")
        self.title("Editar produto" if product else "Novo produto")
        scale = parent.ui_scale
        width, height = round(570 * scale), round(560 * scale)
        x = parent.winfo_x() + max(20, (parent.winfo_width() - width) // 2)
        y = parent.winfo_y() + max(20, (parent.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#f7f8f5")
        self.columnconfigure(0, weight=1)

        header = tk.Frame(self, bg="#15251f", height=82)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="CADASTRO DE PRODUTO", bg="#15251f", fg="#8eb29f", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=28, pady=(18, 2))
        tk.Label(header, text="Editar produto" if product else "Novo produto", bg="#15251f", fg="white", font=("Georgia", 20)).pack(anchor="w", padx=28)

        form = tk.Frame(self, bg="#f7f8f5")
        form.grid(row=1, column=0, sticky="nsew", padx=28, pady=20)
        form.columnconfigure((0, 1), weight=1)
        self.name = self.field(form, "Nome do produto *", 0, 0, product["name"] if product else "", span=2)
        self.category = self.field(form, "Categoria", 2, 0, product["category"] if product else "")
        self.unit = tk.StringVar(value=product["unit"] if product else "un")
        self.label(form, "Unidade", 2, 1)
        ttk.Combobox(form, textvariable=self.unit, values=("un", "kg", "g", "l", "ml", "cx", "pct"), state="readonly").grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(0, 13), ipady=5)
        self.cost = self.field(form, "Custo unitário", 4, 0, "" if not product or product["cost"] is None else str(product["cost"]))
        self.minimum = self.field(form, "Estoque mínimo", 4, 1, str(product["minimum"] if product else 0))
        self.label(form, "Foto opcional", 6, 0)
        ttk.Button(form, text="Escolher foto...", command=self.choose_photo).grid(row=7, column=0, sticky="ew", padx=(0, 8), pady=(0, 13), ipady=4)
        self.photo_label = tk.Label(form, text=Path(self.photo_path.get()).name if self.photo_path.get() else "Nenhuma foto", bg="#f7f8f5", fg="#718078", anchor="w")
        self.photo_label.grid(row=7, column=1, sticky="ew", padx=(8, 0))
        self.label(form, "Observações", 8, 0)
        self.notes = tk.Text(form, height=4, font=("Segoe UI", 10), relief="solid", bd=1)
        self.notes.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        if product:
            self.notes.insert("1.0", product["notes"])
        actions = tk.Frame(form, bg="#f7f8f5")
        actions.grid(row=10, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="Cancelar", command=self.destroy).pack(side="left", padx=5, ipadx=10, ipady=5)
        ttk.Button(actions, text="Salvar produto", style="Accent.TButton", command=self.save).pack(side="left", padx=5, ipadx=10, ipady=5)
        self.name.focus_set()

    @staticmethod
    def label(parent: tk.Widget, text: str, row: int, column: int) -> None:
        tk.Label(parent, text=text, bg="#f7f8f5", fg="#46524c", font=("Segoe UI", 9, "bold"), anchor="w").grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 8, 8 if column == 0 else 0), pady=(0, 5))

    def field(self, parent: tk.Widget, label: str, row: int, column: int, value: str, span: int = 1) -> ttk.Entry:
        self.label(parent, label, row, column)
        entry = ttk.Entry(parent)
        entry.insert(0, value)
        entry.grid(row=row + 1, column=column, columnspan=span, sticky="ew", padx=(0 if column == 0 else 8, 0), pady=(0, 13), ipady=6)
        return entry

    def choose_photo(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="Escolher foto", filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp")])
        if selected:
            self.photo_path.set(selected)
            self.photo_label.config(text=Path(selected).name)

    def save(self) -> None:
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning(APP_NAME, "Informe o nome do produto.", parent=self)
            return
        try:
            cost = float(self.cost.get().replace(",", ".")) if self.cost.get().strip() else None
            minimum = float(self.minimum.get().replace(",", ".") or 0)
            if (cost is not None and cost < 0) or minimum < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_NAME, "Custo e estoque mínimo devem ser números positivos.", parent=self)
            return
        photo = self.photo_path.get()
        if photo and (not self.product or photo != self.product["photo"]):
            source = Path(photo)
            if source.exists():
                destination = app_data_dir() / "fotos" / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix.lower()}"
                shutil.copy2(source, destination)
                photo = str(destination)
        self.result = {"name": name, "category": self.category.get().strip(), "unit": self.unit.get(), "cost": cost, "minimum": minimum, "photo": photo, "notes": self.notes.get("1.0", "end").strip()}
        self.destroy()


class EstoqueApp(tk.Tk):
    BG, INK, MUTED, GREEN, DARK, SOFT, LINE, ORANGE, RED = "#f7f8f5", "#18231f", "#718078", "#246b50", "#15251f", "#eaf4ef", "#e1e7e3", "#bf6a32", "#b34242"

    def __init__(self) -> None:
        super().__init__()
        self.update_idletasks()
        dpi = float(self.winfo_fpixels("1i"))
        self.ui_scale = max(1.0, min(dpi / 96.0, 3.0))
        self.tk.call("tk", "scaling", dpi / 72.0)
        self.db = Database()
        self.title(f"{APP_NAME} — v{APP_VERSION}")
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(round(screen_w * 0.88), round(2500 * self.ui_scale))
        height = min(round(screen_h * 0.86), round(1400 * self.ui_scale))
        width = max(width, min(screen_w, round(1080 * self.ui_scale)))
        height = max(height, min(screen_h, round(680 * self.ui_scale)))
        x, y = max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(screen_w, round(920 * self.ui_scale)), min(screen_h, round(600 * self.ui_scale)))
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.configure_styles()
        self.tabs: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.selected_product_id: int | None = None
        self.photo_cache: ImageTk.PhotoImage | None = None
        self.icon_images: dict[str, ImageTk.PhotoImage] = {}
        self.create_icons()
        self.iconphoto(True, self.icon_images["app"])
        self.build_shell()
        self.show_tab("products")

    def configure_styles(self) -> None:
        row_height = max(36, round(34 * self.ui_scale))
        self.style.configure("Treeview", font=("Segoe UI", 10), rowheight=row_height, background="white", fieldbackground="white", foreground=self.INK, borderwidth=0)
        self.style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#edf2ef", foreground="#4d5b54", relief="flat", padding=8)
        self.style.map("Treeview", background=[("selected", "#dcece3")], foreground=[("selected", self.INK)])
        self.style.configure("Accent.TButton", background=self.GREEN, foreground="white", borderwidth=0, font=("Segoe UI", 9, "bold"))
        self.style.map("Accent.TButton", background=[("active", "#19533d")])
        self.style.configure("TButton", font=("Segoe UI", 9), padding=6)
        self.style.configure("TEntry", fieldbackground="white", padding=5)
        self.style.configure("TCombobox", fieldbackground="white", padding=5)

    def create_icons(self) -> None:
        """Draw crisp tab icons at the active monitor's DPI."""
        import math

        logical = 24
        px = max(24, round(logical * self.ui_scale))
        stroke = max(2, round(2 * self.ui_scale))

        def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
            image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            return image, ImageDraw.Draw(image)

        def point(value: float) -> int:
            return round(value / logical * px)

        color = "#b9d3c5"
        icons: dict[str, Image.Image] = {}
        image, draw = canvas()
        draw.rounded_rectangle((point(3), point(6), point(21), point(20)), radius=point(2), outline=color, width=stroke)
        draw.line((point(3), point(10), point(12), point(14), point(21), point(10)), fill=color, width=stroke)
        draw.line((point(12), point(14), point(12), point(20)), fill=color, width=stroke)
        draw.line((point(3), point(6), point(12), point(2), point(21), point(6)), fill=color, width=stroke)
        icons["products"] = image
        image, draw = canvas()
        for y, length in ((5, 16), (10, 13), (15, 18), (20, 10)):
            draw.rounded_rectangle((point(3), point(y - 1), point(length), point(y + 1)), radius=point(1), fill=color)
        draw.line((point(20), point(4), point(20), point(20)), fill=color, width=stroke)
        draw.line((point(17), point(17), point(20), point(20), point(23), point(17)), fill=color, width=stroke)
        icons["stock"] = image
        image, draw = canvas()
        draw.line((point(4), point(8), point(19), point(8)), fill=color, width=stroke)
        draw.line((point(16), point(5), point(19), point(8), point(16), point(11)), fill=color, width=stroke)
        draw.line((point(20), point(16), point(5), point(16)), fill=color, width=stroke)
        draw.line((point(8), point(13), point(5), point(16), point(8), point(19)), fill=color, width=stroke)
        icons["movements"] = image
        image, draw = canvas()
        draw.ellipse((point(7), point(7), point(17), point(17)), outline=color, width=stroke)
        draw.ellipse((point(10), point(10), point(14), point(14)), fill=color)
        for angle in range(0, 360, 45):
            a, cx, cy = math.radians(angle), point(12), point(12)
            draw.line((cx + point(6) * math.cos(a), cy + point(6) * math.sin(a), cx + point(9) * math.cos(a), cy + point(9) * math.sin(a)), fill=color, width=stroke)
        icons["settings"] = image
        size = max(72, px * 3)
        app_icon = Image.new("RGBA", (size, size), "#15251f")
        app_draw = ImageDraw.Draw(app_icon)
        app_draw.rounded_rectangle((size * .18, size * .25, size * .82, size * .76), radius=size * .08, outline="#dceade", width=max(4, size // 18))
        app_draw.line((size * .18, size * .38, size * .5, size * .52, size * .82, size * .38), fill="#dceade", width=max(4, size // 18))
        app_draw.line((size * .5, size * .52, size * .5, size * .76), fill="#dceade", width=max(4, size // 18))
        icons["app"] = app_icon
        self.icon_images = {name: ImageTk.PhotoImage(image) for name, image in icons.items()}

    def build_shell(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        side = tk.Frame(self, bg=self.DARK, width=220)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        tk.Label(side, image=self.icon_images["app"], bg=self.DARK).pack(anchor="w", padx=25, pady=(25, 0))
        tk.Label(side, text="Estoque Fácil", bg=self.DARK, fg="white", font=("Georgia", 17)).pack(anchor="w", padx=25, pady=(3, 0))
        tk.Label(side, text="CONTROLE SIMPLES", bg=self.DARK, fg="#82998f", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=25, pady=(2, 28))
        for key, label in (("products", "Produtos"), ("stock", "Estoque atual"), ("movements", "Movimentações"), ("settings", "Configurações")):
            button = tk.Button(side, text=label, image=self.icon_images[key], compound="left", anchor="w", bg=self.DARK, fg="#bdcbc5", activebackground="#203a30", activeforeground="white", bd=0, font=("Segoe UI", 10), padx=20, pady=12, command=lambda k=key: self.show_tab(k))
            button.pack(fill="x", padx=9, pady=2)
            self.nav_buttons[key] = button
        tk.Label(side, text=f"●  Dados locais\n     Versão {APP_VERSION}", justify="left", bg=self.DARK, fg="#7fa18f", font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=25, pady=24)

        self.main = tk.Frame(self, bg=self.BG)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_columnconfigure(0, weight=1)
        self.header = tk.Frame(self.main, bg="white", height=94, highlightbackground=self.LINE, highlightthickness=1)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_propagate(False)
        self.header_title = tk.Label(self.header, text="", bg="white", fg=self.INK, font=("Georgia", 23))
        self.header_title.pack(anchor="w", padx=34, pady=(35, 0))
        self.body = tk.Frame(self.main, bg=self.BG)
        self.body.grid(row=1, column=0, sticky="nsew", padx=30, pady=25)
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)

    def show_tab(self, key: str) -> None:
        for frame in self.tabs.values():
            frame.grid_remove()
        if key not in self.tabs:
            builders = {"products": self.build_products, "stock": self.build_stock, "movements": self.build_movements, "settings": self.build_settings}
            self.tabs[key] = builders[key]()
        self.tabs[key].grid(row=0, column=0, sticky="nsew")
        for name, button in self.nav_buttons.items():
            button.config(bg="#203a30" if name == key else self.DARK, fg="white" if name == key else "#bdcbc5")
        titles = {"products": "Produtos", "stock": "Estoque atual", "movements": "Movimentações e inventário", "settings": "Configurações"}
        self.header_title.config(text=titles[key])
        {"products": self.refresh_products, "stock": self.refresh_stock, "movements": self.refresh_movements, "settings": lambda: None}[key]()

    def panel(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(parent, bg="white", highlightbackground=self.LINE, highlightthickness=1)

    def section_title(self, parent: tk.Widget, title: str, subtitle: str) -> tk.Frame:
        head = tk.Frame(parent, bg="white")
        head.pack(fill="x", padx=20, pady=17)
        tk.Label(head, text=title, bg="white", fg=self.INK, font=("Georgia", 16)).pack(anchor="w")
        tk.Label(head, text=subtitle, bg="white", fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))
        return head

    def build_products(self) -> tk.Frame:
        frame = tk.Frame(self.body, bg=self.BG)
        top = tk.Frame(frame, bg=self.BG)
        top.pack(fill="x", pady=(0, 14))
        self.product_search = ttk.Entry(top)
        self.product_search.pack(side="left", fill="x", expand=True, ipady=5)
        self.product_search.insert(0, "")
        self.product_search.bind("<KeyRelease>", lambda _e: self.refresh_products())
        ttk.Button(top, text="Novo produto", style="Accent.TButton", command=self.new_product).pack(side="left", padx=(10, 0), ipadx=12, ipady=4)
        ttk.Button(top, text="Editar", command=self.edit_product).pack(side="left", padx=(7, 0), ipady=4)
        ttk.Button(top, text="Excluir", command=self.delete_product).pack(side="left", padx=(7, 0), ipady=4)
        panel = self.panel(frame)
        panel.pack(fill="both", expand=True)
        self.section_title(panel, "Produtos cadastrados", "Selecione um produto para editar ou excluir.")
        columns = ("name", "category", "unit", "cost", "minimum", "stock")
        self.product_tree = ttk.Treeview(panel, columns=columns, show="headings", selectmode="browse")
        heads = {"name": "Produto", "category": "Categoria", "unit": "Un.", "cost": "Custo", "minimum": "Mínimo", "stock": "Saldo"}
        for col, text in heads.items():
            self.product_tree.heading(col, text=text)
        self.product_tree.column("name", width=230); self.product_tree.column("category", width=160); self.product_tree.column("unit", width=55, anchor="center"); self.product_tree.column("cost", width=100, anchor="e"); self.product_tree.column("minimum", width=80, anchor="e"); self.product_tree.column("stock", width=90, anchor="e")
        self.product_tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.product_tree.bind("<Double-1>", lambda _e: self.edit_product())
        return frame

    def refresh_products(self) -> None:
        if not hasattr(self, "product_tree"): return
        self.product_tree.delete(*self.product_tree.get_children())
        search = self.product_search.get() if hasattr(self, "product_search") else ""
        for p in self.db.products(search):
            cost = "—" if p["cost"] is None else f"R$ {p['cost']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            self.product_tree.insert("", "end", iid=str(p["id"]), values=(p["name"], p["category"] or "—", p["unit"], cost, self.num(p["minimum"]), self.num(p["stock"])))

    def chosen_product_id(self) -> int | None:
        selected = self.product_tree.selection() if hasattr(self, "product_tree") else ()
        return int(selected[0]) if selected else None

    def new_product(self) -> None:
        dialog = ProductDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.db.save_product(dialog.result)
            self.refresh_all()

    def edit_product(self) -> None:
        product_id = self.chosen_product_id()
        if not product_id:
            messagebox.showinfo(APP_NAME, "Selecione um produto para editar.", parent=self); return
        dialog = ProductDialog(self, self.db.product(product_id))
        self.wait_window(dialog)
        if dialog.result:
            self.db.save_product(dialog.result, product_id)
            self.refresh_all()

    def delete_product(self) -> None:
        product_id = self.chosen_product_id()
        if not product_id:
            messagebox.showinfo(APP_NAME, "Selecione um produto para excluir.", parent=self); return
        product = self.db.product(product_id)
        if messagebox.askyesno(APP_NAME, f"Excluir o produto “{product['name']}”?", parent=self):
            if not self.db.delete_product(product_id):
                messagebox.showwarning(APP_NAME, "Produtos com histórico de movimentações não podem ser excluídos.", parent=self)
            self.refresh_all()

    def build_stock(self) -> tk.Frame:
        frame = tk.Frame(self.body, bg=self.BG)
        cards = tk.Frame(frame, bg=self.BG)
        cards.pack(fill="x", pady=(0, 14))
        self.stock_cards = []
        for title in ("Produtos", "Unidades em estoque", "Abaixo do mínimo", "Valor estimado"):
            card = self.panel(cards); card.pack(side="left", fill="both", expand=True, padx=(0, 10))
            tk.Label(card, text=title, bg="white", fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(13, 2))
            value = tk.Label(card, text="0", bg="white", fg=self.INK, font=("Georgia", 19)); value.pack(anchor="w", padx=16, pady=(0, 13)); self.stock_cards.append(value)
        panel = self.panel(frame); panel.pack(fill="both", expand=True)
        head = self.section_title(panel, "Visão atual do estoque", "Saldo calculado a partir de todas as entradas, saídas e contagens.")
        self.stock_search = ttk.Entry(head); self.stock_search.pack(side="right", padx=(0, 2), ipadx=35, ipady=4); self.stock_search.bind("<KeyRelease>", lambda _e: self.refresh_stock())
        columns = ("name", "category", "stock", "unit", "minimum", "status", "value")
        self.stock_tree = ttk.Treeview(panel, columns=columns, show="headings")
        for col, label in (("name", "Produto"), ("category", "Categoria"), ("stock", "Saldo atual"), ("unit", "Un."), ("minimum", "Mínimo"), ("status", "Situação"), ("value", "Valor estimado")):
            self.stock_tree.heading(col, text=label)
        self.stock_tree.column("name", width=220); self.stock_tree.column("category", width=140); self.stock_tree.column("stock", width=95, anchor="e"); self.stock_tree.column("unit", width=50, anchor="center"); self.stock_tree.column("minimum", width=75, anchor="e"); self.stock_tree.column("status", width=105, anchor="center"); self.stock_tree.column("value", width=120, anchor="e")
        self.stock_tree.tag_configure("ok", foreground=self.GREEN); self.stock_tree.tag_configure("low", foreground=self.ORANGE); self.stock_tree.tag_configure("zero", foreground=self.RED)
        self.stock_tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        return frame

    def refresh_stock(self) -> None:
        if not hasattr(self, "stock_tree"): return
        products = self.db.products(self.stock_search.get() if hasattr(self, "stock_search") else "")
        self.stock_tree.delete(*self.stock_tree.get_children())
        total_units, low, total_value = 0.0, 0, 0.0
        for p in products:
            stock = float(p["stock"]); total_units += stock; total_value += stock * float(p["cost"] or 0)
            if stock <= 0: status, tag = "Sem estoque", "zero"
            elif stock <= float(p["minimum"]): status, tag = "Estoque baixo", "low"
            else: status, tag = "Normal", "ok"
            if tag != "ok": low += 1
            value = f"R$ {stock * float(p['cost'] or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            self.stock_tree.insert("", "end", iid=f"s{p['id']}", values=(p["name"], p["category"] or "—", self.num(stock), p["unit"], self.num(p["minimum"]), status, value), tags=(tag,))
        if self.stock_cards:
            self.stock_cards[0].config(text=str(len(products))); self.stock_cards[1].config(text=self.num(total_units)); self.stock_cards[2].config(text=str(low), fg=self.ORANGE if low else self.INK); self.stock_cards[3].config(text=(f"R$ {total_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")))

    def build_movements(self) -> tk.Frame:
        frame = tk.Frame(self.body, bg=self.BG)
        frame.grid_columnconfigure(1, weight=1); frame.grid_rowconfigure(0, weight=1)
        form_panel = self.panel(frame); form_panel.grid(row=0, column=0, sticky="ns", padx=(0, 15)); form_panel.configure(width=330); form_panel.grid_propagate(False)
        self.section_title(form_panel, "Registrar movimentação", "Inventário informa a quantidade contada.")
        form = tk.Frame(form_panel, bg="white"); form.pack(fill="both", padx=20)
        self.movement_type = tk.StringVar(value="entrada")
        self.movement_product = tk.StringVar(); self.movement_quantity = tk.StringVar(); self.movement_date = tk.StringVar(value=date.today().isoformat()); self.movement_reason = tk.StringVar()
        self.form_label(form, "Operação"); ttk.Combobox(form, textvariable=self.movement_type, state="readonly", values=("entrada", "saida", "ajuste", "inventario")).pack(fill="x", pady=(0, 12), ipady=4)
        self.form_label(form, "Produto"); self.movement_product_combo = ttk.Combobox(form, textvariable=self.movement_product, state="readonly"); self.movement_product_combo.pack(fill="x", pady=(0, 12), ipady=4); self.movement_product_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_current_stock())
        self.current_stock_label = tk.Label(form, text="Saldo atual: —", bg=self.SOFT, fg=self.GREEN, font=("Segoe UI", 10, "bold"), padx=10, pady=9); self.current_stock_label.pack(fill="x", pady=(0, 12))
        self.form_label(form, "Quantidade / nova contagem"); ttk.Entry(form, textvariable=self.movement_quantity).pack(fill="x", pady=(0, 12), ipady=5)
        self.form_label(form, "Data (AAAA-MM-DD)"); ttk.Entry(form, textvariable=self.movement_date).pack(fill="x", pady=(0, 12), ipady=5)
        self.form_label(form, "Motivo ou observação"); ttk.Entry(form, textvariable=self.movement_reason).pack(fill="x", pady=(0, 18), ipady=5)
        ttk.Button(form, text="Registrar movimentação", style="Accent.TButton", command=self.register_movement).pack(fill="x", ipady=6)
        history = self.panel(frame); history.grid(row=0, column=1, sticky="nsew")
        head = self.section_title(history, "Histórico", "As alterações mais recentes aparecem primeiro.")
        self.history_filter = tk.StringVar(value="todos"); combo = ttk.Combobox(head, textvariable=self.history_filter, state="readonly", values=("todos", "entrada", "saida", "ajuste", "inventario"), width=14); combo.pack(side="right", ipady=4); combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_movements())
        cols = ("date", "product", "type", "quantity", "stock", "reason")
        self.history_tree = ttk.Treeview(history, columns=cols, show="headings")
        for col, label in (("date", "Data"), ("product", "Produto"), ("type", "Operação"), ("quantity", "Alteração"), ("stock", "Saldo"), ("reason", "Observação")): self.history_tree.heading(col, text=label)
        self.history_tree.column("date", width=85); self.history_tree.column("product", width=160); self.history_tree.column("type", width=90); self.history_tree.column("quantity", width=80, anchor="e"); self.history_tree.column("stock", width=70, anchor="e"); self.history_tree.column("reason", width=220)
        self.history_tree.tag_configure("positive", foreground=self.GREEN); self.history_tree.tag_configure("negative", foreground=self.RED)
        self.history_tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        return frame

    def form_label(self, parent: tk.Widget, text: str) -> None:
        tk.Label(parent, text=text, bg="white", fg="#46524c", font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 4))

    def movement_product_map(self) -> dict[str, int]:
        return {f"{p['name']}  [{p['unit']}]": int(p["id"]) for p in self.db.products()}

    def update_current_stock(self) -> None:
        product_id = self.movement_product_map().get(self.movement_product.get())
        self.current_stock_label.config(text=f"Saldo atual: {self.num(self.db.stock(product_id))}" if product_id else "Saldo atual: —")

    def register_movement(self) -> None:
        product_id = self.movement_product_map().get(self.movement_product.get())
        if not product_id:
            messagebox.showwarning(APP_NAME, "Selecione um produto.", parent=self); return
        try:
            amount = float(self.movement_quantity.get().replace(",", "."))
            if amount < 0: raise ValueError
            datetime.strptime(self.movement_date.get(), "%Y-%m-%d")
            self.db.add_movement(product_id, self.movement_type.get(), amount, self.movement_date.get(), self.movement_reason.get().strip())
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error) if str(error) else "Revise a quantidade e a data.", parent=self); return
        self.movement_quantity.set(""); self.movement_reason.set(""); self.movement_date.set(date.today().isoformat())
        self.refresh_all(); self.update_current_stock()
        messagebox.showinfo(APP_NAME, "Movimentação registrada.", parent=self)

    def refresh_movements(self) -> None:
        if not hasattr(self, "history_tree"): return
        product_map = self.movement_product_map(); values = list(product_map.keys()); self.movement_product_combo["values"] = values
        if self.movement_product.get() not in values: self.movement_product.set("")
        self.history_tree.delete(*self.history_tree.get_children())
        labels = {"entrada": "Entrada", "saida": "Saída", "ajuste": "Ajuste", "inventario": "Inventário"}
        for m in self.db.movements(self.history_filter.get()):
            qty = float(m["quantity"]); sign = "+" if qty > 0 else ""
            self.history_tree.insert("", "end", values=(datetime.strptime(m["movement_date"], "%Y-%m-%d").strftime("%d/%m/%Y"), m["name"], labels[m["type"]], f"{sign}{self.num(qty)} {m['unit']}", f"{self.num(m['resulting_stock'])} {m['unit']}", m["reason"]), tags=("positive" if qty > 0 else "negative",))

    def build_settings(self) -> tk.Frame:
        frame = tk.Frame(self.body, bg=self.BG)
        for title, text, action, button in (
            ("Atualizações", f"Versão instalada: {APP_VERSION}. Consulte as versões publicadas no GitHub.", self.check_updates, "Buscar atualização"),
            ("Backup dos dados", "Salve uma cópia do banco de dados antes de trocar de computador.", self.backup, "Baixar backup"),
            ("Restaurar backup", "Substitui os dados atuais por um arquivo de backup anterior.", self.restore, "Restaurar backup"),
            ("Apagar dados", "Remove definitivamente todos os produtos e movimentações.", self.clear_data, "Apagar todos os dados"),
        ):
            panel = self.panel(frame); panel.pack(fill="x", pady=(0, 13))
            content = tk.Frame(panel, bg="white"); content.pack(fill="x", padx=22, pady=18)
            tk.Label(content, text=title, bg="white", fg=self.INK, font=("Georgia", 15)).pack(anchor="w")
            tk.Label(content, text=text, bg="white", fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 12))
            ttk.Button(content, text=button, style="Accent.TButton" if title != "Apagar dados" else "TButton", command=action).pack(anchor="w", ipadx=9, ipady=4)
        return frame

    def check_updates(self) -> None:
        try:
            request = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(request, timeout=10) as response:
                release = json.load(response)
            latest = release["tag_name"].lstrip("v")
            if latest == APP_VERSION:
                messagebox.showinfo(APP_NAME, "Você já está usando a versão mais recente.", parent=self)
            elif messagebox.askyesno(APP_NAME, f"A versão {latest} está disponível. Abrir a página para baixar?", parent=self):
                webbrowser.open(release["html_url"])
        except (urllib.error.URLError, KeyError, TimeoutError):
            messagebox.showerror(APP_NAME, "Não foi possível consultar o GitHub agora.", parent=self)

    def backup(self) -> None:
        target = filedialog.asksaveasfilename(parent=self, title="Salvar backup", defaultextension=".db", initialfile=f"estoque-backup-{date.today().isoformat()}.db", filetypes=[("Backup do Estoque Fácil", "*.db")])
        if target:
            self.db.backup(Path(target)); messagebox.showinfo(APP_NAME, "Backup salvo com sucesso.", parent=self)

    def restore(self) -> None:
        source = filedialog.askopenfilename(parent=self, title="Restaurar backup", filetypes=[("Backup do Estoque Fácil", "*.db")])
        if source and messagebox.askyesno(APP_NAME, "Os dados atuais serão substituídos. Continuar?", parent=self):
            try:
                self.db.restore(Path(source)); self.refresh_all(); messagebox.showinfo(APP_NAME, "Backup restaurado.", parent=self)
            except (sqlite3.DatabaseError, OSError):
                messagebox.showerror(APP_NAME, "O arquivo selecionado não é um backup válido.", parent=self)

    def clear_data(self) -> None:
        if messagebox.askyesno(APP_NAME, "Apagar definitivamente todos os produtos e movimentações?", icon="warning", parent=self):
            self.db.clear(); self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_products(); self.refresh_stock(); self.refresh_movements()

    @staticmethod
    def num(value: float) -> str:
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".").replace(".", ",")

    def on_close(self) -> None:
        self.db.db.close(); self.destroy()


if __name__ == "__main__":
    try:
        enable_windows_dpi_awareness()
        app = EstoqueApp()
        app.mainloop()
    except Exception as error:
        log_path = app_data_dir() / "erro.log"
        log_path.write_text(f"{datetime.now().isoformat()}\n{error!r}\n", encoding="utf-8")
        raise
