"""Regenerate the two binary files the desktop build needs.

    resources/icon.ico              the app icon, drawn from app/static/brand.svg
    backend/selfcheck/sample.pdf    the tiny PDF the frozen backend's self-check reads

Both are committed, so this only needs running when the brand mark changes or the
self-check needs a different sample. It uses PyMuPDF, which the app already depends on:

    python desktop-app/scripts/make-assets.py
"""

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf

DESKTOP = Path(__file__).resolve().parents[1]
BRAND = DESKTOP.parent / "app" / "static" / "brand.svg"
ICON = DESKTOP / "resources" / "icon.ico"
SAMPLE = DESKTOP / "backend" / "selfcheck" / "sample.pdf"

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# The mark fills its 40-unit canvas edge to edge. A little air keeps it from looking
# cramped next to other icons on the taskbar.
ICON_MARGIN = 0.04
CANVAS = 1024  # drawing units; every icon size divides it, so each render is exact


def _colour(value):
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def draw_mark():
    """Redraw brand.svg as a vector page with a transparent background.

    MuPDF's own SVG reader flattens the rounded corners, so the shapes are read from
    the file and drawn here with real curves. Only what the mark uses is understood:
    rounded rectangles, and a path of horizontal strokes. Anything else is an error,
    so a redesigned mark can't quietly produce a wrong icon.
    """
    root = ET.parse(BRAND).getroot()
    _, _, box_width, box_height = map(float, root.get("viewBox").split())
    scale = CANVAS * (1 - 2 * ICON_MARGIN) / max(box_width, box_height)
    offset = CANVAS * ICON_MARGIN

    def place(x, y):
        return pymupdf.Point(offset + x * scale, offset + y * scale)

    document = pymupdf.open()
    shape = document.new_page(width=CANVAS, height=CANVAS).new_shape()
    for element in root:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "rect":
            x, y, width, height = (float(element.get(name)) for name in ("x", "y", "width", "height"))
            radius = float(element.get("rx", 0))
            shape.draw_rect(pymupdf.Rect(place(x, y), place(x + width, y + height)),
                            radius=(radius / width, radius / height) if radius else None)
            shape.finish(fill=_colour(element.get("fill")), color=None)
        elif tag == "path":
            commands = re.findall(r"([Mh])\s*(-?[\d.]+)(?:[\s,]+(-?[\d.]+))?", element.get("d"))
            if "".join(c for c, _, _ in commands) != re.sub(r"[^A-Za-z]", "", element.get("d")):
                raise SystemExit(f"brand.svg path uses commands this script cannot draw: {element.get('d')}")
            for command, first, second in commands:
                if command == "M":
                    x, y = float(first), float(second)
                else:
                    shape.draw_line(place(x, y), place(x + float(first), y))
                    x += float(first)
            shape.finish(color=_colour(element.get("stroke")), width=float(element.get("stroke-width")) * scale,
                         lineCap=1 if element.get("stroke-linecap") == "round" else 0, closePath=False)
        else:
            raise SystemExit(f"brand.svg has a <{tag}> element this script cannot draw")
    shape.commit()
    return document


def render_mark(document, size):
    """The brand mark as a `size` x `size` RGBA pixmap."""
    pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(size / CANVAS, size / CANVAS), alpha=True)
    assert (pixmap.width, pixmap.height) == (size, size), (pixmap.width, pixmap.height)
    return pixmap


def _dib(pixmap):
    """A pixmap as the 32-bit bitmap an .ico stores for its smaller sizes."""
    width, height = pixmap.width, pixmap.height
    rows = []
    # MuPDF premultiplies alpha; an .ico bitmap wants the plain colours.
    samples = _straight_alpha(pixmap)
    stride = width * 4
    for y in range(height - 1, -1, -1):  # bottom-up
        row = bytearray(samples[y * stride:(y + 1) * stride])
        row[0::4], row[2::4] = row[2::4], row[0::4]  # RGBA -> BGRA
        rows.append(bytes(row))
    mask_row = bytes(((width + 31) // 32) * 4)  # all-zero AND mask: the alpha channel decides
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    return header + b"".join(rows) + mask_row * height


def _straight_alpha(pixmap):
    samples = bytearray(pixmap.samples)
    for i in range(0, len(samples), 4):
        alpha = samples[i + 3]
        if 0 < alpha < 255:
            for channel in range(3):
                samples[i + channel] = min(255, round(samples[i + channel] * 255 / alpha))
    return bytes(samples)


def make_icon():
    images = []
    mark = draw_mark()
    for size in ICON_SIZES:
        pixmap = render_mark(mark, size)
        # Windows wants PNG for the 256 px image and plain bitmaps below it.
        images.append((size, pixmap.tobytes("png") if size >= 256 else _dib(pixmap)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = [], []
    for size, blob in images:
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    ICON.parent.mkdir(parents=True, exist_ok=True)
    ICON.write_bytes(header + b"".join(entries) + b"".join(blobs))
    print(f"{ICON.relative_to(DESKTOP)}: {ICON.stat().st_size:,} bytes, sizes {', '.join(map(str, ICON_SIZES))}")


def make_sample_pdf():
    """One page with headings, body text and a picture large enough to count as a figure."""
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 96), "Cell biology", fontsize=22, fontname="hebo")
    page.insert_textbox(
        pymupdf.Rect(72, 120, 523, 200),
        "Mitochondria are the organelles that release energy from glucose. They are found in "
        "almost every eukaryotic cell, and a cell that needs more energy holds more of them.",
        fontsize=11, fontname="helv",
    )
    page.insert_text((72, 236), "Figure 1. A mitochondrion", fontsize=13, fontname="hebo")

    # A small drawn picture, embedded as a raster image so figure extraction finds it.
    width, height = 240, 160
    picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    picture.clear_with(255)
    for x in range(width):
        for y in range(height):
            dx, dy = (x - width / 2) / (width / 2 - 8), (y - height / 2) / (height / 2 - 8)
            distance = dx * dx + dy * dy
            if distance <= 1:
                picture.set_pixel(x, y, (209, 74, 45) if distance > 0.78 else (241, 185, 168))
    page.insert_image(pymupdf.Rect(72, 252, 72 + width, 252 + height), pixmap=picture)
    page.insert_textbox(
        pymupdf.Rect(72, 430, 523, 500),
        "The inner membrane is folded into cristae, which is where most ATP is made.",
        fontsize=11, fontname="helv",
    )
    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    document.save(SAMPLE, garbage=4, deflate=True)
    document.close()
    print(f"{SAMPLE.relative_to(DESKTOP)}: {SAMPLE.stat().st_size:,} bytes")


if __name__ == "__main__":
    make_icon()
    make_sample_pdf()
