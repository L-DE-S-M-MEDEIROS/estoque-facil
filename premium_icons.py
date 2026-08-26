from __future__ import annotations

import math
import sys
from pathlib import Path
from PIL import Image, ImageDraw
import customtkinter as ctk

LIGHT_ICON = "#506178"
DARK_ICON = "#8BD5FF"


def _resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def application_icon_path() -> Path:
    return _resource_path("assets/brand/estoque-bolsas-baby.ico")


def _app_icon_image() -> Image.Image:
    source_path = _resource_path("assets/brand/icone-aplicativo.png")
    with Image.open(source_path) as source_file:
        return source_file.convert("RGBA")


def brand_mark(display_width: int = 86) -> ctk.CTkImage:
    """Create a theme-aware monochrome mark from the Bolsas Baby company logo."""
    source_path = _resource_path("assets/brand/icone-bolsas-baby.png")
    with Image.open(source_path) as source_file:
        source = source_file.convert("RGBA")

    illustration = source.crop((0, 0, source.width, round(source.height * .65)))
    bounds = illustration.getchannel("A").getbbox()
    if bounds:
        illustration = illustration.crop(bounds)
    alpha = illustration.getchannel("A")
    light = Image.new("RGBA", illustration.size, (0, 0, 0, 0))
    dark = Image.new("RGBA", illustration.size, (255, 255, 255, 0))
    light.putalpha(alpha)
    dark.putalpha(alpha)
    display_height = max(24, round(display_width * illustration.height / illustration.width))
    return ctk.CTkImage(light_image=light, dark_image=dark, size=(display_width, display_height))


def _draw_icon(name: str, color: str, size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 24
    width = max(8, round(size * 0.065))
    p = lambda value: round(value * unit)
    line = dict(fill=color, width=width, joint="curve")

    if name == "products":
        draw.rounded_rectangle((p(3), p(6), p(21), p(20)), radius=p(2), outline=color, width=width)
        draw.line((p(3), p(10), p(12), p(14), p(21), p(10)), **line)
        draw.line((p(12), p(14), p(12), p(20)), **line)
        draw.line((p(3), p(6), p(12), p(2), p(21), p(6)), **line)
    elif name == "stock":
        for y, length in ((5, 16), (10, 13), (15, 18), (20, 10)):
            draw.rounded_rectangle((p(3), p(y - .7), p(length), p(y + .7)), radius=p(.7), fill=color)
        draw.line((p(20), p(4), p(20), p(20)), **line)
        draw.line((p(17), p(17), p(20), p(20), p(23), p(17)), **line)
    elif name == "movements":
        draw.line((p(4), p(8), p(19), p(8)), **line)
        draw.line((p(16), p(5), p(19), p(8), p(16), p(11)), **line)
        draw.line((p(20), p(16), p(5), p(16)), **line)
        draw.line((p(8), p(13), p(5), p(16), p(8), p(19)), **line)
    elif name == "kit_conversion":
        draw.rounded_rectangle((p(3), p(4), p(10), p(11)), radius=p(1.2), outline=color, width=width)
        draw.rounded_rectangle((p(14), p(13), p(21), p(20)), radius=p(1.2), outline=color, width=width)
        draw.line((p(11), p(7), p(19), p(7), p(19), p(11)), **line)
        draw.line((p(16), p(9), p(19), p(12), p(22), p(9)), **line)
        draw.line((p(13), p(17), p(5), p(17), p(5), p(13)), **line)
        draw.line((p(8), p(15), p(5), p(12), p(2), p(15)), **line)
    elif name == "operation":
        draw.rounded_rectangle((p(3), p(4), p(21), p(20)), radius=p(2), outline=color, width=width)
        draw.line((p(7), p(9), p(17), p(9)), **line)
        draw.line((p(7), p(15), p(14), p(15)), **line)
        draw.line((p(17), p(13), p(17), p(17)), **line)
    elif name == "simulation":
        draw.rounded_rectangle((p(4), p(3), p(20), p(21)), radius=p(2), outline=color, width=width)
        draw.line((p(8), p(8), p(16), p(8)), **line)
        draw.line((p(8), p(12), p(16), p(12)), **line)
        draw.line((p(8), p(16), p(12), p(16)), **line)
        draw.line((p(16), p(14), p(16), p(18)), **line)
        draw.line((p(14), p(16), p(18), p(16)), **line)
    elif name == "group":
        draw.rounded_rectangle((p(3), p(4), p(21), p(10)), radius=p(1.5), outline=color, width=width)
        draw.rounded_rectangle((p(3), p(13), p(21), p(19)), radius=p(1.5), outline=color, width=width)
        draw.line((p(7), p(10), p(7), p(13)), **line)
        draw.line((p(17), p(10), p(17), p(13)), **line)
    elif name == "user":
        draw.ellipse((p(8), p(3), p(16), p(11)), outline=color, width=width)
        draw.arc((p(4), p(10), p(20), p(23)), 190, 350, fill=color, width=width)
    elif name == "registration":
        for x, y in ((4, 4), (14, 4), (4, 14)):
            draw.rounded_rectangle((p(x), p(y), p(x+6), p(y+6)), radius=p(1.3), outline=color, width=width)
        draw.line((p(17), p(14), p(17), p(22)), **line)
        draw.line((p(13), p(18), p(21), p(18)), **line)
    elif name == "count":
        draw.rounded_rectangle((p(5), p(4), p(20), p(22)), radius=p(2), outline=color, width=width)
        draw.rounded_rectangle((p(9), p(2), p(16), p(7)), radius=p(1.5), outline=color, width=width)
        for y in (10, 15, 20):
            draw.line((p(8), p(y), p(10), p(y + 2), p(13), p(y - 2)), **line)
            draw.line((p(15), p(y), p(18), p(y)), **line)
    elif name == "settings":
        draw.ellipse((p(7), p(7), p(17), p(17)), outline=color, width=width)
        draw.ellipse((p(10), p(10), p(14), p(14)), fill=color)
        for angle in range(0, 360, 45):
            a = math.radians(angle)
            draw.line((p(12) + p(6) * math.cos(a), p(12) + p(6) * math.sin(a), p(12) + p(9) * math.cos(a), p(12) + p(9) * math.sin(a)), **line)
    elif name == "plus":
        draw.line((p(12), p(4), p(12), p(20)), **line); draw.line((p(4), p(12), p(20), p(12)), **line)
    elif name == "search":
        draw.ellipse((p(3), p(3), p(16), p(16)), outline=color, width=width); draw.line((p(15), p(15), p(22), p(22)), **line)
    elif name == "edit":
        draw.line((p(5), p(19), p(8), p(13), p(17), p(4), p(21), p(8), p(12), p(17), p(5), p(19)), **line)
    elif name == "trash":
        draw.line((p(4), p(7), p(20), p(7)), **line); draw.line((p(8), p(7), p(9), p(21), p(17), p(21), p(18), p(7)), **line); draw.line((p(9), p(4), p(15), p(4)), **line)
    elif name == "download":
        draw.line((p(12), p(3), p(12), p(16)), **line); draw.line((p(7), p(11), p(12), p(16), p(17), p(11)), **line); draw.line((p(4), p(21), p(20), p(21)), **line)
    elif name == "upload":
        draw.line((p(12), p(17), p(12), p(4)), **line); draw.line((p(7), p(9), p(12), p(4), p(17), p(9)), **line); draw.line((p(4), p(21), p(20), p(21)), **line)
    elif name == "refresh":
        draw.arc((p(3), p(3), p(21), p(21)), 35, 325, fill=color, width=width); draw.line((p(17), p(3), p(21), p(7), p(16), p(8)), **line)
    elif name == "print":
        draw.rounded_rectangle((p(5), p(2), p(19), p(9)), radius=p(1), outline=color, width=width)
        draw.rounded_rectangle((p(3), p(8), p(21), p(18)), radius=p(2), outline=color, width=width)
        draw.rectangle((p(6), p(14), p(18), p(22)), fill=(0, 0, 0, 0), outline=color, width=width)
        draw.ellipse((p(17), p(10), p(19), p(12)), fill=color)
    elif name == "collapse":
        draw.line((p(5), p(15), p(12), p(8), p(19), p(15)), **line)
    elif name == "expand":
        draw.line((p(5), p(9), p(12), p(16), p(19), p(9)), **line)
    elif name == "calendar":
        draw.rounded_rectangle((p(3), p(5), p(21), p(21)), radius=p(2), outline=color, width=width)
        draw.line((p(3), p(10), p(21), p(10)), **line)
        draw.line((p(8), p(3), p(8), p(7)), **line); draw.line((p(16), p(3), p(16), p(7)), **line)
        for x in (7, 12, 17):
            for y in (14, 18): draw.ellipse((p(x-.7), p(y-.7), p(x+.7), p(y+.7)), fill=color)
    return image


def icon(name: str, display_size: int = 22) -> ctk.CTkImage:
    return ctk.CTkImage(light_image=_draw_icon(name, LIGHT_ICON), dark_image=_draw_icon(name, DARK_ICON), size=(display_size, display_size))


def app_icon(size: int = 256) -> Image.Image:
    return _app_icon_image().resize((size, size), Image.Resampling.LANCZOS)
