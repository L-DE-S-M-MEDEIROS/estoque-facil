from __future__ import annotations

import ctypes
import json
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

from premium_icons import app_icon, icon
from premium_widgets import MaskedDateEntry

APP_NAME = "Estoque Fácil"
APP_VERSION = "0.5.0"
GITHUB_REPO = "L-DE-S-M-MEDEIROS/estoque-facil"

COLORS = {
    "background": ("#F6F7F9", "#0B0F16"),
    "surface": ("#FFFFFF", "#121824"),
    "surface_hover": ("#F0F4F8", "#192232"),
    "surface_alt": ("#EEF3F8", "#171E2B"),
    "text": ("#202936", "#F3F7FB"),
    "muted": ("#748092", "#91A0B5"),
    "border": ("#DEE5EC", "#263244"),
    "accent": ("#4A9FD8", "#36BFFA"),
    "accent_hover": ("#368BC3", "#67D3FF"),
    "accent_soft": ("#E5F3FC", "#102B3D"),
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


def data_dir() -> Path:
    folder = Path.home() / "AppData" / "Local" / "EstoqueFacil"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fotos").mkdir(exist_ok=True)
    return folder


def fmt_number(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


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
                quantity REAL NOT NULL, resulting_stock REAL NOT NULL,
                movement_date TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT);
            CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id);
            CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(movement_date DESC);
        """)
        self.db.commit()

    def products(self, search: str = "") -> list[sqlite3.Row]:
        term = f"%{search.strip()}%"
        return self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id
            WHERE p.name LIKE ? OR p.category LIKE ? GROUP BY p.id ORDER BY p.name COLLATE NOCASE""", (term, term)).fetchall()

    def product(self, product_id: int) -> sqlite3.Row | None:
        return self.db.execute("""SELECT p.*,COALESCE(SUM(m.quantity),0) stock
            FROM products p LEFT JOIN movements m ON m.product_id=p.id WHERE p.id=? GROUP BY p.id""", (product_id,)).fetchone()

    def save_product(self, values: dict, product_id: int | None = None) -> None:
        fields = (values["name"], values["category"], values["unit"], values["cost"], values["minimum"], values["photo"], values["notes"])
        if product_id:
            self.db.execute("UPDATE products SET name=?,category=?,unit=?,cost=?,minimum=?,photo=?,notes=? WHERE id=?", fields + (product_id,))
        else:
            self.db.execute("INSERT INTO products(name,category,unit,cost,minimum,photo,notes,created_at) VALUES(?,?,?,?,?,?,?,?)", fields + (datetime.now().isoformat(timespec="seconds"),))
        self.db.commit()

    def delete_product(self, product_id: int) -> bool:
        if self.db.execute("SELECT 1 FROM movements WHERE product_id=? LIMIT 1", (product_id,)).fetchone():
            return False
        self.db.execute("DELETE FROM products WHERE id=?", (product_id,)); self.db.commit(); return True

    def stock(self, product_id: int) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(quantity),0) value FROM movements WHERE product_id=?", (product_id,)).fetchone()["value"])

    def add_movement(self, product_id: int, kind: str, informed: float, movement_date: str, reason: str) -> None:
        current = self.stock(product_id)
        delta = -informed if kind == "saida" else informed
        if kind in ("ajuste", "inventario"):
            delta = informed - current
        if current + delta < 0:
            raise ValueError("A saída é maior que o saldo disponível.")
        if abs(delta) < .0000001:
            raise ValueError("A quantidade informada já é o saldo atual.")
        self.db.execute("INSERT INTO movements(product_id,type,quantity,resulting_stock,movement_date,reason,created_at) VALUES(?,?,?,?,?,?,?)", (product_id, kind, delta, current + delta, movement_date, reason or ("Contagem de inventário" if kind == "inventario" else "Sem observação"), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def movements(self, kind: str = "todos") -> list[sqlite3.Row]:
        where, args = ("", ()) if kind == "todos" else ("WHERE m.type=?", (kind,))
        return self.db.execute(f"""SELECT m.*,p.name,p.unit FROM movements m JOIN products p ON p.id=m.product_id
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
        self.unit = self.field(form, "Unidade", 2, 1, product["unit"] if product else "un", combo=["un", "kg", "g", "l", "ml", "cx", "pct"])
        self.cost = self.field(form, "Custo unitário", 4, 0, "" if not product or product["cost"] is None else str(product["cost"]))
        self.minimum = self.field(form, "Estoque mínimo", 4, 1, str(product["minimum"] if product else 0))
        self.photo = product["photo"] if product else ""
        ctk.CTkLabel(form, text="Foto opcional", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(8, 6))
        self.photo_button = ctk.CTkButton(form, text=Path(self.photo).name if self.photo else "Escolher foto", image=icon("upload", 18), fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.choose_photo)
        self.photo_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 16), ipady=3)
        ctk.CTkLabel(form, text="Observações", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=8, column=0, sticky="w", pady=(0, 6))
        self.notes = ctk.CTkTextbox(form, height=90, corner_radius=9, border_width=1, border_color=COLORS["border"], fg_color=COLORS["surface"])
        self.notes.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(0, 20)); self.notes.insert("1.0", product["notes"] if product else "")
        actions = ctk.CTkFrame(form, fg_color="transparent"); actions.grid(row=10, column=0, columnspan=2, sticky="e")
        ctk.CTkButton(actions, text="Cancelar", width=110, fg_color=COLORS["surface_alt"], hover_color=COLORS["surface_hover"], text_color=COLORS["text"], command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(actions, text="Salvar produto", width=145, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.save).pack(side="left", padx=6)
        self.name.focus_set()

    def field(self, parent, label, row, column, value, span=1, combo=None):
        padx = (0, 8) if column == 0 else (8, 0)
        ctk.CTkLabel(parent, text=label, text_color=COLORS["text"], font=ctk.CTkFont("Inter", 11, "bold")).grid(row=row, column=column, sticky="w", padx=padx, pady=(0, 6))
        widget = ctk.CTkComboBox(parent, values=combo, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"], button_color=COLORS["accent"]) if combo else ctk.CTkEntry(parent, corner_radius=9, border_color=COLORS["border"], fg_color=COLORS["surface"])
        widget.grid(row=row+1, column=column, columnspan=span, sticky="ew", padx=padx, pady=(0, 16), ipady=3); widget.set(value) if combo else widget.insert(0, value); return widget

    def choose_photo(self):
        selected = filedialog.askopenfilename(parent=self, title="Escolher foto", filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp")])
        if selected: self.photo = selected; self.photo_button.configure(text=Path(selected).name)

    def save(self):
        name = self.name.get().strip()
        if not name: messagebox.showwarning(APP_NAME, "Informe o nome do produto.", parent=self); return
        try:
            cost = float(self.cost.get().replace(",", ".")) if self.cost.get().strip() else None
            minimum = float(self.minimum.get().replace(",", ".") or 0)
            if (cost is not None and cost < 0) or minimum < 0: raise ValueError
        except ValueError: messagebox.showwarning(APP_NAME, "Custo e estoque mínimo devem ser números positivos.", parent=self); return
        photo = self.photo
        if photo and (not self.product or photo != self.product["photo"]):
            source = Path(photo)
            if source.exists():
                target = data_dir()/"fotos"/f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix.lower()}"; shutil.copy2(source, target); photo = str(target)
        self.result = {"name": name, "category": self.category.get().strip(), "unit": self.unit.get(), "cost": cost, "minimum": minimum, "photo": photo, "notes": self.notes.get("1.0", "end").strip()}; self.destroy()


class EstoqueApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=COLORS["background"])
        dpi = float(self.winfo_fpixels("1i")); self.ui_scale = max(1, min(dpi/96, 3)); self.tk.call("tk", "scaling", dpi/72)
        self.settings_path = data_dir()/"settings.json"; self.settings = self.load_settings(); ctk.set_appearance_mode(self.settings.get("theme", "Light"))
        self.db = Database(); self.title(f"{APP_NAME} — v{APP_VERSION}")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight(); w, h = round(sw*.9), round(sh*.88); self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}"); self.minsize(min(sw, round(1050*self.ui_scale)), min(sh, round(680*self.ui_scale)))
        self.iconphoto(True, ImageTk.PhotoImage(app_icon(256))); self.protocol("WM_DELETE_WINDOW", self.close)
        self.icons = {name: icon(name, 22) for name in ("products", "stock", "movements", "settings", "plus", "search", "edit", "trash", "download", "upload", "refresh")}
        self.nav_buttons = {}; self.pages = {}; self.build_shell(); self.show_page("stock")

    def load_settings(self):
        try: return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {"theme": "Light"}

    def save_settings(self): self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")

    def build_shell(self):
        self.grid_columnconfigure(1, weight=1); self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=265, corner_radius=0, fg_color=COLORS["sidebar"]); self.sidebar.grid(row=0, column=0, sticky="nsw"); self.sidebar.grid_propagate(False)
        logo = ctk.CTkFrame(self.sidebar, fg_color="transparent"); logo.pack(fill="x", padx=24, pady=(28, 34))
        ctk.CTkLabel(logo, text="EF", width=46, height=46, corner_radius=12, fg_color=COLORS["accent"], text_color="#FFFFFF", font=ctk.CTkFont("Inter", 15, "bold")).pack(side="left")
        brand = ctk.CTkFrame(logo, fg_color="transparent"); brand.pack(side="left", padx=13)
        ctk.CTkLabel(brand, text="Estoque Fácil", text_color=COLORS["text"], font=ctk.CTkFont("Inter", 16, "bold")).pack(anchor="w")
        ctk.CTkLabel(brand, text="CONTROLE INTELIGENTE", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 9)).pack(anchor="w", pady=(2,0))
        for key, label in (("stock","Estoque atual"),("movements","Movimentações"),("products","Produtos"),("settings","Configurações")):
            button = ctk.CTkButton(self.sidebar, text=label, image=self.icons[key], compound="left", anchor="w", height=48, corner_radius=10, fg_color="transparent", hover_color=COLORS["surface_hover"], text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 13, "bold"), command=lambda k=key:self.show_page(k))
            button.pack(fill="x", padx=16, pady=4); self.nav_buttons[key]=button
        ctk.CTkLabel(self.sidebar, text=f"●  Dados locais protegidos\n    Versão {APP_VERSION}", justify="left", text_color=COLORS["muted"], font=ctk.CTkFont("Inter", 10)).pack(side="bottom", anchor="w", padx=26, pady=28)
        self.content = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0); self.content.grid(row=0,column=1,sticky="nsew"); self.content.grid_columnconfigure(0,weight=1); self.content.grid_rowconfigure(0,weight=1)

    def show_page(self,key):
        for page in self.pages.values(): page.grid_remove()
        if key not in self.pages: self.pages[key]={"products":self.products_page,"stock":self.stock_page,"movements":self.movements_page,"settings":self.settings_page}[key]()
        self.pages[key].grid(row=0,column=0,sticky="nsew",padx=32,pady=28)
        for name,button in self.nav_buttons.items(): button.configure(fg_color=COLORS["accent_soft"] if name==key else "transparent", text_color=COLORS["accent"] if name==key else COLORS["muted"])
        {"products":self.refresh_products,"stock":self.refresh_stock,"movements":self.refresh_movements,"settings":lambda:None}[key]()

    def table(self,parent,columns,headings,widths):
        tree=ttk.Treeview(parent,columns=columns,show="headings",selectmode="browse")
        for col,label,width in zip(columns,headings,widths): tree.heading(col,text=label); tree.column(col,width=width,anchor="e" if col in ("cost","minimum","stock","quantity","value") else "w")
        return tree

    def configure_tables(self):
        dark=ctk.get_appearance_mode()=="Dark"; bg="#121824" if dark else "#FFFFFF"; fg="#F3F7FB" if dark else "#202936"; head="#192232" if dark else "#EEF3F8"; selected="#203C52" if dark else "#DDEFFC"
        style=ttk.Style(self); style.theme_use("clam"); style.configure("Treeview",background=bg,fieldbackground=bg,foreground=fg,rowheight=max(38,round(34*self.ui_scale)),borderwidth=0,font=("Inter",10)); style.configure("Treeview.Heading",background=head,foreground=fg,relief="flat",font=("Inter",9,"bold"),padding=10); style.map("Treeview",background=[("selected",selected)],foreground=[("selected",fg)])

    def products_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent"); PageTitle(page,"Produtos","Cadastre e organize os itens do seu estoque.").pack(fill="x",pady=(0,22))
        toolbar=ctk.CTkFrame(page,fg_color="transparent");toolbar.pack(fill="x",pady=(0,16)); self.product_search=ctk.CTkEntry(toolbar,placeholder_text="Buscar por nome ou categoria...",width=430,height=44,corner_radius=10,border_color=COLORS["border"],fg_color=COLORS["surface"]);self.product_search.pack(side="left");self.product_search.bind("<KeyRelease>",lambda e:self.refresh_products())
        for text,name,cmd,color in (("Novo produto","plus",self.new_product,COLORS["accent"]),("Editar","edit",self.edit_product,COLORS["surface_alt"]),("Excluir","trash",self.delete_product,COLORS["surface_alt"])):
            ctk.CTkButton(toolbar,text=text,image=self.icons[name],height=44,corner_radius=10,fg_color=color,hover_color=COLORS["accent_hover"] if name=="plus" else COLORS["surface_hover"],text_color="#FFFFFF" if name=="plus" else COLORS["text"],command=cmd).pack(side="left",padx=(10,0))
        card=Card(page);card.pack(fill="both",expand=True); self.product_tree=self.table(card,("name","category","unit","cost","minimum","stock"),("Produto","Categoria","Un.","Custo","Mínimo","Saldo"),(260,170,60,110,90,90));self.product_tree.pack(fill="both",expand=True,padx=20,pady=20);self.product_tree.bind("<Double-1>",lambda e:self.edit_product());self.configure_tables();return page

    def refresh_products(self):
        if not hasattr(self,"product_tree"):return
        self.product_tree.delete(*self.product_tree.get_children()); search=self.product_search.get() if hasattr(self,"product_search") else ""
        for p in self.db.products(search):self.product_tree.insert("","end",iid=str(p["id"]),values=(p["name"],p["category"]or"—",p["unit"],fmt_money(p["cost"]),fmt_number(p["minimum"]),fmt_number(p["stock"])))

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
        for title in ("Produtos","Unidades em estoque","Abaixo do mínimo","Valor estimado"):
            card=Card(cards,height=108);card.pack(side="left",fill="both",expand=True,padx=(0,12));card.pack_propagate(False);ctk.CTkLabel(card,text=title,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",padx=18,pady=(17,3));label=ctk.CTkLabel(card,text="0",text_color=COLORS["text"],font=ctk.CTkFont("Inter",22,"bold"));label.pack(anchor="w",padx=18);self.stock_cards.append(label)
        card=Card(page);card.pack(fill="both",expand=True);bar=ctk.CTkFrame(card,fg_color="transparent");bar.pack(fill="x",padx=20,pady=(18,8));ctk.CTkLabel(bar,text="Posição do estoque",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(side="left");self.stock_search=ctk.CTkEntry(bar,placeholder_text="Filtrar produtos...",width=260,height=38,corner_radius=9);self.stock_search.pack(side="right");self.stock_search.bind("<KeyRelease>",lambda e:self.refresh_stock())
        self.stock_tree=self.table(card,("name","category","stock","unit","minimum","status","value"),("Produto","Categoria","Saldo atual","Un.","Mínimo","Situação","Valor estimado"),(230,150,100,55,80,115,125));self.stock_tree.pack(fill="both",expand=True,padx=20,pady=(8,20));self.configure_tables();return page

    def refresh_stock(self):
        if not hasattr(self,"stock_tree"):return
        items=self.db.products(self.stock_search.get() if hasattr(self,"stock_search") else "");self.stock_tree.delete(*self.stock_tree.get_children());units=low=value=0
        for p in items:
            stock=float(p["stock"]);units+=stock;value+=stock*float(p["cost"]or 0);status="Sem estoque" if stock<=0 else "Estoque baixo" if stock<=float(p["minimum"]) else "Normal";low+=status!="Normal";self.stock_tree.insert("","end",values=(p["name"],p["category"]or"—",fmt_number(stock),p["unit"],fmt_number(p["minimum"]),status,fmt_money(stock*float(p["cost"]or 0))))
        for label,text in zip(self.stock_cards,(str(len(items)),fmt_number(units),str(low),fmt_money(value))):label.configure(text=text)

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
        history=Card(body);history.grid(row=0,column=1,sticky="nsew");bar=ctk.CTkFrame(history,fg_color="transparent");bar.pack(fill="x",padx=20,pady=18);ctk.CTkLabel(bar,text="Histórico",text_color=COLORS["text"],font=ctk.CTkFont("Inter",16,"bold")).pack(side="left");self.history_filter=tk.StringVar(value="todos");ctk.CTkOptionMenu(bar,variable=self.history_filter,values=["todos","entrada","saida","ajuste","inventario"],width=150,fg_color=COLORS["surface_alt"],button_color=COLORS["surface_hover"],text_color=COLORS["text"],command=lambda _v:self.refresh_movements()).pack(side="right")
        self.history_tree=self.table(history,("date","product","type","quantity","stock","reason"),("Data","Produto","Operação","Alteração","Saldo","Observação"),(90,170,95,90,80,230));self.history_tree.pack(fill="both",expand=True,padx=20,pady=(0,20));self.configure_tables();return page

    def product_map(self):return {f"{p['name']}  [{p['unit']}]":int(p["id"]) for p in self.db.products()}
    def update_current_stock(self):
        pid=self.product_map().get(self.m_product.get());self.current_stock.configure(text=f"Saldo atual: {fmt_number(self.db.stock(pid))}" if pid else "Saldo atual: —")
    def register_movement(self):
        pid=self.product_map().get(self.m_product.get())
        if not pid:messagebox.showwarning(APP_NAME,"Selecione um produto.",parent=self);return
        try:amount=float(self.m_quantity.get().replace(",","."));movement_date=self.m_date_entry.get_date();self.db.add_movement(pid,self.m_type.get(),amount,movement_date.isoformat(),self.m_reason.get().strip())
        except ValueError as error:messagebox.showwarning(APP_NAME,str(error)or"Revise a quantidade e a data.",parent=self);return
        self.m_quantity.set("");self.m_reason.set("");self.m_date_entry.set_date(date.today());self.refresh_all();self.update_current_stock();messagebox.showinfo(APP_NAME,"Movimentação registrada.",parent=self)
    def refresh_movements(self):
        if not hasattr(self,"history_tree"):return
        mapping=self.product_map();self.m_product_combo.configure(values=list(mapping)or[""]);self.history_tree.delete(*self.history_tree.get_children());labels={"entrada":"Entrada","saida":"Saída","ajuste":"Ajuste","inventario":"Inventário"}
        for m in self.db.movements(self.history_filter.get()):qty=float(m["quantity"]);self.history_tree.insert("","end",values=(datetime.strptime(m["movement_date"],"%Y-%m-%d").strftime("%d/%m/%y"),m["name"],labels[m["type"]],f"{'+' if qty>0 else ''}{fmt_number(qty)} {m['unit']}",f"{fmt_number(m['resulting_stock'])} {m['unit']}",m["reason"]))

    def settings_page(self):
        page=ctk.CTkFrame(self.content,fg_color="transparent");PageTitle(page,"Configurações","Personalize a aparência e proteja seus dados.").pack(fill="x",pady=(0,22))
        appearance=Card(page);appearance.pack(fill="x",pady=(0,16));row=ctk.CTkFrame(appearance,fg_color="transparent");row.pack(fill="x",padx=22,pady=20);ctk.CTkLabel(row,text="Tema da interface",text_color=COLORS["text"],font=ctk.CTkFont("Inter",15,"bold")).pack(anchor="w");ctk.CTkLabel(row,text="Escolha entre o modo claro off-white e o modo escuro em grafite.",text_color=COLORS["muted"],font=ctk.CTkFont("Inter",11)).pack(anchor="w",pady=(4,12));self.theme_selector=ctk.CTkSegmentedButton(row,values=["Light","Dark"],command=self.change_theme,selected_color=COLORS["accent"],selected_hover_color=COLORS["accent_hover"]);self.theme_selector.pack(anchor="w");self.theme_selector.set(self.settings.get("theme","Light"))
        actions=ctk.CTkFrame(page,fg_color="transparent");actions.pack(fill="both",expand=True);actions.grid_columnconfigure((0,1),weight=1)
        for index,(title,text,icon_name,command,button) in enumerate((("Atualizações",f"Versão instalada: {APP_VERSION}.","refresh",self.check_updates,"Buscar atualização"),("Backup dos dados","Salve uma cópia segura do banco local.","download",self.backup,"Baixar backup"),("Restaurar backup","Substitua os dados por um backup anterior.","upload",self.restore,"Restaurar backup"),("Apagar dados","Remove produtos e movimentações definitivamente.","trash",self.clear_data,"Apagar dados"))):
            card=Card(actions);card.grid(row=index//2,column=index%2,sticky="nsew",padx=(0 if index%2==0 else 8,8 if index%2==0 else 0),pady=8);ctk.CTkLabel(card,text=title,image=self.icons[icon_name],compound="left",text_color=COLORS["text"],font=ctk.CTkFont("Inter",14,"bold")).pack(anchor="w",padx=20,pady=(20,5));ctk.CTkLabel(card,text=text,text_color=COLORS["muted"],font=ctk.CTkFont("Inter",10)).pack(anchor="w",padx=20);ctk.CTkButton(card,text=button,height=38,corner_radius=9,fg_color=COLORS["surface_alt"],hover_color=COLORS["surface_hover"],text_color=COLORS["danger"] if title=="Apagar dados" else COLORS["text"],command=command).pack(anchor="w",padx=20,pady=20)
        return page

    def change_theme(self,value):
        self.settings["theme"]=value;self.save_settings();ctk.set_appearance_mode(value);self.configure_tables()
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
    def refresh_all(self):self.refresh_products();self.refresh_stock();self.refresh_movements()
    def close(self):self.db.db.close();self.destroy()


if __name__=="__main__":
    try:enable_dpi_awareness();EstoqueApp().mainloop()
    except Exception as error:(data_dir()/"erro.log").write_text(f"{datetime.now().isoformat()}\n{error!r}\n",encoding="utf-8");raise
