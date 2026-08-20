from __future__ import annotations

import calendar
from datetime import date, datetime
import math
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from premium_icons import icon


CONFIDENCE_TIERS = (
    (90, "Máxima", ("#2478C4", "#38BDF8")),
    (61, "Alta", ("#27845E", "#4ADE80")),
    (41, "Média", ("#B77912", "#FFD166")),
    (0, "Baixa", ("#C94B4B", "#FF6B6B")),
)

STOCK_QUANTITY_TIERS = (
    (100, ("#2478C4", "#38BDF8")),
    (50, ("#27845E", "#4ADE80")),
    (31, ("#A87500", "#FFD166")),
    (1, ("#D45A32", "#FF8A5C")),
    (0, ("#8F2433", "#FF5D6C")),
)


def confidence_tier(score: int) -> tuple[str, tuple[str, str]]:
    """Return the label and theme-aware color for a confidence score."""
    bounded = max(0, min(100, int(score)))
    return next((label, color) for minimum, label, color in CONFIDENCE_TIERS if bounded >= minimum)


def stock_quantity_color(quantity: float) -> tuple[str, str]:
    """Return the requested theme-aware stock color for a quantity."""
    value = max(0.0, float(quantity))
    return next(color for minimum, color in STOCK_QUANTITY_TIERS if value >= minimum)


def _appearance_color(value: str | tuple[str, str]) -> str:
    if isinstance(value, tuple):
        return value[1] if ctk.get_appearance_mode() == "Dark" else value[0]
    return value


def mini_confidence_gauge(score: int, colors: dict, width: int = 66, height: int = 28) -> Image.Image:
    """Render a compact, antialiased confidence dial for a table cell."""
    scale = 4
    pixel_width, pixel_height = width * scale, height * scale
    image = Image.new("RGBA", (pixel_width, pixel_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center_x, center_y = pixel_width // 2, round(pixel_height * .92)
    radius = min(round(pixel_width * .39), round(pixel_height * .82))
    stroke = max(8, round(pixel_width * .085))
    box = (center_x - radius, center_y - radius, center_x + radius, center_y + radius)
    bounded = max(0, min(100, int(score)))
    _level, tier_color = confidence_tier(bounded)
    color = _appearance_color(tier_color)
    draw.arc(box, start=180, end=360, fill=_appearance_color(colors["border"]), width=stroke)
    draw.arc(box, start=180, end=180 + bounded * 1.8, fill=color, width=stroke)
    angle = math.pi - math.pi * bounded / 100
    needle_radius = radius - round(stroke * .55)
    draw.line(
        (
            center_x,
            center_y,
            center_x + needle_radius * math.cos(angle),
            center_y - needle_radius * math.sin(angle),
        ),
        fill=color,
        width=max(5, stroke // 3),
    )
    hub = max(6, stroke // 2)
    draw.ellipse((center_x - hub, center_y - hub, center_x + hub, center_y + hub), fill=color)
    return image.resize((width, height), Image.Resampling.LANCZOS)


class TreeConfidenceOverlay:
    """Place crisp confidence dials over a ttk.Treeview confidence column."""

    def __init__(self, tree, colors: dict, column: str = "confidence", activate=None):
        self.tree, self.colors, self.column, self.activate = tree, colors, column, activate
        self.scores: dict[str, int] = {}
        self.labels: list[tk.Label] = []
        self.images: list[ImageTk.PhotoImage] = []
        self._job = None
        for event in ("<Configure>", "<MouseWheel>", "<Button-4>", "<Button-5>", "<KeyRelease>", "<ButtonRelease-1>", "<<TreeviewSelect>>"):
            self.tree.bind(event, self.schedule, add="+")

    def set_scores(self, scores: dict[int | str, int]):
        self.scores = {str(item_id): int(score) for item_id, score in scores.items()}
        self.schedule()

    def schedule(self, _event=None):
        if not self.tree.winfo_exists():
            return
        if self._job is not None:
            self.tree.after_cancel(self._job)
        self._job = self.tree.after_idle(self.redraw)

    def _select(self, item_id: str):
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.event_generate("<<TreeviewSelect>>")

    def _open(self, item_id: str):
        self._select(item_id)
        if self.activate:
            self.activate()

    def _scroll(self, event):
        direction = -1 if event.delta > 0 else 1
        self.tree.yview_scroll(direction, "units")
        self.schedule()
        return "break"

    def redraw(self):
        self._job = None
        for label in self.labels:
            label.destroy()
        self.labels.clear()
        self.images.clear()
        if not self.tree.winfo_exists():
            return

        selected = set(self.tree.selection())
        normal_background = _appearance_color(self.colors["surface"])
        selected_background = "#203C52" if ctk.get_appearance_mode() == "Dark" else "#DDEFFC"
        for item_id, score in self.scores.items():
            bounds = self.tree.bbox(item_id, self.column)
            if not bounds:
                continue
            x, y, cell_width, cell_height = bounds
            image_width = max(44, min(70, cell_width - 10))
            image_height = max(22, min(29, cell_height - 5))
            rendered = mini_confidence_gauge(score, self.colors, image_width, image_height)
            photo = ImageTk.PhotoImage(rendered)
            label = tk.Label(
                self.tree,
                image=photo,
                background=selected_background if item_id in selected else normal_background,
                borderwidth=0,
                highlightthickness=0,
                cursor="hand2",
            )
            label.bind("<Button-1>", lambda _event, current=item_id: self._select(current))
            label.bind("<Double-Button-1>", lambda _event, current=item_id: self._open(current))
            label.bind("<MouseWheel>", self._scroll)
            label.place(x=x + (cell_width - image_width) // 2, y=y + (cell_height - image_height) // 2)
            self.labels.append(label)
            self.images.append(photo)


class TreeStockOverlay(TreeConfidenceOverlay):
    """Color the numeric stock value without changing the rest of the row."""

    def __init__(self, tree, colors: dict, column: str = "stock"):
        super().__init__(tree, colors, column=column)
        self.quantities: dict[str, tuple[float, str]] = {}

    def set_quantities(self, quantities: dict[int | str, tuple[float, str]]):
        self.quantities = {
            str(item_id): (float(quantity), str(display))
            for item_id, (quantity, display) in quantities.items()
        }
        self.schedule()

    def redraw(self):
        self._job = None
        for label in self.labels:
            label.destroy()
        self.labels.clear()
        self.images.clear()
        if not self.tree.winfo_exists():
            return

        selected = set(self.tree.selection())
        normal_background = _appearance_color(self.colors["surface"])
        selected_background = "#203C52" if ctk.get_appearance_mode() == "Dark" else "#DDEFFC"
        for item_id, (quantity, display) in self.quantities.items():
            bounds = self.tree.bbox(item_id, self.column)
            if not bounds:
                continue
            x, y, cell_width, cell_height = bounds
            label = tk.Label(
                self.tree,
                text=display,
                foreground=_appearance_color(stock_quantity_color(quantity)),
                background=selected_background if item_id in selected else normal_background,
                borderwidth=0,
                highlightthickness=0,
                font=("Inter", 11, "bold"),
                cursor="hand2",
            )
            label.bind("<Button-1>", lambda _event, current=item_id: self._select(current))
            label.bind("<MouseWheel>", self._scroll)
            label.place(x=x, y=y, width=cell_width, height=cell_height)
            self.labels.append(label)


class ConfidenceGauge(ctk.CTkFrame):
    """High-resolution semicircular confidence gauge for Light and Dark themes."""

    def __init__(self, master, colors: dict, width: int = 250, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.colors = colors
        self.display_width = width
        self.display_height = max(112, round(width * .48))
        self.score: int | None = None
        self._image = None
        self.image_label = ctk.CTkLabel(self, text="", width=self.display_width, height=self.display_height)
        self.image_label.pack()
        self.value_label = ctk.CTkLabel(
            self,
            text="—",
            text_color=colors["muted"],
            font=ctk.CTkFont("Inter", 18, "bold"),
        )
        self.value_label.pack(pady=(0, 1))
        self.level_label = ctk.CTkLabel(
            self,
            text="Selecione um produto",
            text_color=colors["muted"],
            font=ctk.CTkFont("Inter", 10, "bold"),
        )
        self.level_label.pack()
        self.redraw()

    def _gauge_image(self) -> Image.Image:
        scale = 4
        width, height = self.display_width * scale, self.display_height * scale
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        center_x, center_y = width // 2, round(height * .88)
        radius = min(round(width * .39), round(height * .72))
        stroke = max(12, round(width * .055))
        box = (center_x - radius, center_y - radius, center_x + radius, center_y + radius)
        track = _appearance_color(self.colors["border"])
        draw.arc(box, start=180, end=360, fill=track, width=stroke)

        for tick_score in (0, 25, 50, 75, 100):
            angle = math.pi - math.pi * tick_score / 100
            outer = radius + round(stroke * .58)
            inner = radius + round(stroke * .12)
            draw.line(
                (
                    center_x + inner * math.cos(angle),
                    center_y - inner * math.sin(angle),
                    center_x + outer * math.cos(angle),
                    center_y - outer * math.sin(angle),
                ),
                fill=_appearance_color(self.colors["muted"]),
                width=max(3, stroke // 7),
            )

        if self.score is not None:
            level, tier_color = confidence_tier(self.score)
            color = _appearance_color(tier_color)
            progress_end = 180 + self.score * 1.8
            draw.arc(box, start=180, end=progress_end, fill=color, width=stroke)
            angle = math.pi - math.pi * self.score / 100
            needle_radius = radius - round(stroke * .65)
            end_x = center_x + needle_radius * math.cos(angle)
            end_y = center_y - needle_radius * math.sin(angle)
            draw.line((center_x, center_y, end_x, end_y), fill=color, width=max(10, stroke // 3))
            hub = max(14, stroke // 2)
            draw.ellipse((center_x - hub, center_y - hub, center_x + hub, center_y + hub), fill=color)
            core = max(5, hub // 3)
            draw.ellipse(
                (center_x - core, center_y - core, center_x + core, center_y + core),
                fill=_appearance_color(self.colors["surface"]),
            )

        return image.resize((self.display_width, self.display_height), Image.Resampling.LANCZOS)

    def redraw(self):
        rendered = self._gauge_image()
        self._image = ctk.CTkImage(light_image=rendered, dark_image=rendered, size=(self.display_width, self.display_height))
        self.image_label.configure(image=self._image)
        if self.score is None:
            self.value_label.configure(text="—", text_color=self.colors["muted"])
            self.level_label.configure(text="Selecione um produto", text_color=self.colors["muted"])
            return
        level, color = confidence_tier(self.score)
        self.value_label.configure(text=f"{self.score}%", text_color=color)
        self.level_label.configure(text=level.upper(), text_color=color)

    def set_score(self, score: int | None):
        self.score = None if score is None else max(0, min(100, int(score)))
        self.redraw()


class CalendarPopup(ctk.CTkToplevel):
    def __init__(self, parent, selected: date, callback, colors: dict):
        super().__init__(parent, fg_color=colors["background"])
        self.callback, self.colors = callback, colors
        self.year, self.month = selected.year, selected.month
        self.title("Selecionar data")
        self.geometry(f"340x390+{parent.winfo_rootx()}+{parent.winfo_rooty()+parent.winfo_height()+6}")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=16)
        self.render()

    def render(self):
        for child in self.body.winfo_children(): child.destroy()
        header = ctk.CTkFrame(self.body, fg_color="transparent"); header.pack(fill="x", pady=(0, 14))
        ctk.CTkButton(header, text="‹", width=42, height=36, corner_radius=9, fg_color=self.colors["surface_alt"], hover_color=self.colors["surface_hover"], text_color=self.colors["text"], font=ctk.CTkFont("Inter", 20), command=lambda: self.move(-1)).pack(side="left")
        month_name = ("Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro")[self.month-1]
        ctk.CTkLabel(header, text=f"{month_name} {self.year}", text_color=self.colors["text"], font=ctk.CTkFont("Inter", 14, "bold")).pack(side="left", expand=True)
        ctk.CTkButton(header, text="›", width=42, height=36, corner_radius=9, fg_color=self.colors["surface_alt"], hover_color=self.colors["surface_hover"], text_color=self.colors["text"], font=ctk.CTkFont("Inter", 20), command=lambda: self.move(1)).pack(side="right")
        grid = ctk.CTkFrame(self.body, fg_color="transparent"); grid.pack(fill="both", expand=True)
        for col, name in enumerate(("SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM")):
            grid.grid_columnconfigure(col, weight=1)
            ctk.CTkLabel(grid, text=name, text_color=self.colors["muted"], font=ctk.CTkFont("Inter", 9, "bold")).grid(row=0, column=col, pady=(0, 8))
        for row, week in enumerate(calendar.Calendar().monthdayscalendar(self.year, self.month), start=1):
            grid.grid_rowconfigure(row, weight=1)
            for col, day in enumerate(week):
                if not day: continue
                chosen = date(self.year, self.month, day)
                is_today = chosen == date.today()
                button = ctk.CTkButton(grid, text=str(day), width=36, height=36, corner_radius=9, fg_color=self.colors["accent"] if is_today else "transparent", hover_color=self.colors["accent_soft"], text_color="#FFFFFF" if is_today else self.colors["text"], command=lambda value=chosen: self.choose(value))
                button.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
        ctk.CTkButton(self.body, text="Hoje", height=38, corner_radius=9, fg_color=self.colors["surface_alt"], hover_color=self.colors["surface_hover"], text_color=self.colors["text"], command=lambda: self.choose(date.today())).pack(fill="x", pady=(12, 0))

    def move(self, amount: int):
        month = self.month + amount
        if month < 1: self.month, self.year = 12, self.year - 1
        elif month > 12: self.month, self.year = 1, self.year + 1
        else: self.month = month
        self.render()

    def choose(self, value: date):
        self.callback(value); self.destroy()


class MaskedDateEntry(ctk.CTkFrame):
    """Date input that always preserves the dd/mm/yy separators."""
    def __init__(self, master, colors: dict, initial: date | None = None, **kwargs):
        control_height = kwargs.pop("control_height", 40)
        super().__init__(master, fg_color="transparent", **kwargs)
        self.colors = colors
        self.digits = list((initial or date.today()).strftime("%d%m%y"))
        self.variable = tk.StringVar()
        self.entry = ctk.CTkEntry(self, textvariable=self.variable, height=control_height, corner_radius=9, border_color=colors["border"], fg_color=colors["surface"], font=ctk.CTkFont("Inter", 12))
        self.entry.pack(side="left", fill="x", expand=True)
        self.calendar_icon = icon("calendar", 19)
        ctk.CTkButton(self, text="", image=self.calendar_icon, width=control_height + 2, height=control_height, corner_radius=9, fg_color=colors["surface_alt"], hover_color=colors["surface_hover"], command=self.open_calendar).pack(side="left", padx=(8, 0))
        self.entry.bind("<KeyPress>", self.keypress)
        self.entry.bind("<Button-1>", lambda _e: self.after(1, self.snap_cursor))
        self.refresh(0)

    def formatted(self) -> str:
        digits = self.digits + ["_"] * (6 - len(self.digits))
        return f"{digits[0]}{digits[1]}/{digits[2]}{digits[3]}/{digits[4]}{digits[5]}"

    def refresh(self, digit_position: int | None = None):
        self.variable.set(self.formatted())
        if digit_position is not None:
            indexes = (0, 1, 3, 4, 6, 7, 8)
            self.entry.icursor(indexes[min(digit_position, 6)])

    def cursor_digit_position(self) -> int:
        cursor = self.entry.index(tk.INSERT)
        return sum(1 for index in (0, 1, 3, 4, 6, 7) if index < cursor)

    def snap_cursor(self):
        position = self.cursor_digit_position(); self.refresh(position)

    def keypress(self, event):
        if event.state & 4 and event.keysym.lower() in ("a", "c", "v", "x"): return "break"
        position = self.cursor_digit_position()
        if event.char.isdigit():
            if position < 6:
                while len(self.digits) < position: self.digits.append("_")
                if position < len(self.digits): self.digits[position] = event.char
                else: self.digits.append(event.char)
                self.refresh(position + 1)
            return "break"
        if event.keysym == "BackSpace":
            position = max(0, position - 1)
            if position < len(self.digits): self.digits[position] = "_"
            self.refresh(position); return "break"
        if event.keysym == "Delete":
            if position < len(self.digits): self.digits[position] = "_"
            self.refresh(position); return "break"
        if event.keysym in ("Left", "Right", "Home", "End", "Tab", "ISO_Left_Tab"): return None
        return "break"

    def get_date(self) -> date:
        text = self.formatted()
        if "_" in text: raise ValueError("Preencha a data completa no formato dd/mm/aa.")
        return datetime.strptime(text, "%d/%m/%y").date()

    def set_date(self, value: date):
        self.digits = list(value.strftime("%d%m%y")); self.refresh(6)

    def open_calendar(self):
        try: selected = self.get_date()
        except ValueError: selected = date.today()
        CalendarPopup(self, selected, self.set_date, self.colors)
