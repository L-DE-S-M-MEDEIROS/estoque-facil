from __future__ import annotations

import calendar
from datetime import date, datetime
import tkinter as tk

import customtkinter as ctk

from premium_icons import icon


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
