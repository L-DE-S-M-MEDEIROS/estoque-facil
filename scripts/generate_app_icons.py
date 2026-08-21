from __future__ import annotations

from io import BytesIO
from pathlib import Path
import struct

from PIL import Image, ImageEnhance, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "brand" / "icone-aplicativo.png"
OUTPUTS = (
    ROOT / "assets" / "brand" / "estoque-bolsas-baby.ico",
    ROOT / "src" / "app" / "favicon.ico",
)
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def render_frame(source: Image.Image, size: int) -> Image.Image:
    if size <= 32:
        # Small Windows chrome needs a tighter composition and native-size
        # sharpening; scaling the detailed 1254 px artwork directly looks soft.
        working = source.crop((45, 35, source.width - 45, source.height - 35))
        frame = working.resize((size, size), Image.Resampling.HAMMING)
        frame = frame.filter(ImageFilter.UnsharpMask(radius=.55, percent=210, threshold=1))
        return ImageEnhance.Contrast(frame).enhance(1.1)
    if size <= 48:
        working = source.crop((25, 20, source.width - 25, source.height - 20))
        frame = working.resize((size, size), Image.Resampling.HAMMING)
        return frame.filter(ImageFilter.UnsharpMask(radius=.5, percent=150, threshold=1))
    return source.resize((size, size), Image.Resampling.LANCZOS).filter(
        ImageFilter.UnsharpMask(radius=.4, percent=90, threshold=1)
    )


def png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_ico(source: Image.Image) -> bytes:
    frames = [(size, png_bytes(render_frame(source, size))) for size in SIZES]
    header_size = 6 + 16 * len(frames)
    offset = header_size
    entries = []
    payloads = []
    for size, payload in frames:
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    return struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(payloads)


def main() -> None:
    with Image.open(SOURCE) as source_file:
        source = source_file.convert("RGBA")
    icon = build_ico(source)
    for output in OUTPUTS:
        output.write_bytes(icon)
        print(f"Ícone gerado: {output}")


if __name__ == "__main__":
    main()
