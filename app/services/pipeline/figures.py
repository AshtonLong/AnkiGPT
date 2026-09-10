"""Figures — pull images out of an uploaded PDF and let the vision model read them.

Figure regions are rendered from the page (not just the embedded bitmap) so vector
labels drawn over a raster image survive. Each figure then gets one vision call that
says whether it's worth a card, what kind of figure it is, and which labelled parts and
facts it conveys; `figure_recall` worker tasks are spawned from that analysis and the
image ships inside the .apkg.
"""

import hashlib
import logging

from ..llm import extract_json, image_part, json_schema_format, text_part

logger = logging.getLogger(__name__)

VISION_PROMPT_VERSION = "vision-v1"
MIN_SIDE_PT = 110  # skip icons, bullets, rules
MAX_PAGE_FRACTION = 0.92  # skip full-page scans/backgrounds
RENDER_DPI = 120
MAX_RENDER_SIDE = 1400


def extract_figures(pdf_path, page_start=None, page_end=None, max_figures=24):
    """Return a list of {page, image (png bytes), width, height, hash, bbox} in page order."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        logger.warning("pymupdf not installed; figure extraction disabled")
        return []
    figures = []
    seen = set()
    try:
        with pymupdf.open(pdf_path) as doc:
            total = doc.page_count
            start = max(0, (page_start or 1) - 1)
            end = min(total, page_end or total)
            for pno in range(start, end):
                page = doc[pno]
                page_area = max(1.0, page.rect.width * page.rect.height)
                rects = []
                for info in page.get_images(full=True):
                    xref = info[0]
                    try:
                        for rect in page.get_image_rects(xref):
                            rects.append(rect)
                    except Exception:
                        continue
                # Merge overlapping/adjacent image rects (multi-tile figures).
                rects = _merge_rects(rects)
                for rect in rects:
                    if rect.width < MIN_SIDE_PT or rect.height < MIN_SIDE_PT:
                        continue
                    if (rect.width * rect.height) / page_area > MAX_PAGE_FRACTION:
                        continue
                    # Grow slightly to catch labels sitting just outside the bitmap.
                    clip = pymupdf.Rect(rect.x0 - 6, rect.y0 - 6, rect.x1 + 6, rect.y1 + 6) & page.rect
                    scale = RENDER_DPI / 72.0
                    longest = max(clip.width, clip.height) * scale
                    if longest > MAX_RENDER_SIDE:
                        scale *= MAX_RENDER_SIDE / longest
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
                    png = pix.tobytes("png")
                    digest = hashlib.sha256(png).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    figures.append(
                        {
                            "page": pno + 1,
                            "image": png,
                            "width": pix.width,
                            "height": pix.height,
                            "hash": digest,
                            "bbox": [round(clip.x0), round(clip.y0), round(clip.x1), round(clip.y1)],
                        }
                    )
                    if len(figures) >= max_figures:
                        return figures
    except Exception:
        logger.exception("Figure extraction failed for %s", pdf_path)
    return figures


def _merge_rects(rects, gap=8.0):
    """Union rects that overlap or nearly touch; repeat until stable."""
    rects = [r for r in rects if r.is_valid and not r.is_empty]
    changed = True
    while changed and len(rects) > 1:
        changed = False
        out = []
        while rects:
            cur = rects.pop()
            merged = True
            while merged:
                merged = False
                for i, other in enumerate(rects):
                    grown = type(cur)(cur.x0 - gap, cur.y0 - gap, cur.x1 + gap, cur.y1 + gap)
                    if grown.intersects(other):
                        cur = cur | other
                        rects.pop(i)
                        merged = True
                        changed = True
                        break
            out.append(cur)
        rects = out
    return rects


VISION_SCHEMA = json_schema_format(
    "figure_analysis",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "useful": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["diagram", "chart", "table", "photo", "equation", "map", "decorative", "other"]},
            "caption": {"type": "string"},
            "description": {"type": "string"},
            "parts": {"type": "array", "items": {"type": "string"}},
            "facts": {"type": "array", "items": {"type": "string"}},
            "suggested_cards": {"type": "integer"},
        },
        "required": ["useful", "kind", "caption", "description", "parts", "facts", "suggested_cards"],
    },
)

VISION_SYSTEM = """You analyse a figure from study material for a flashcard generator. Nearby source text is provided for context.

Decide `useful`: true only if a student could be examined on what the figure shows (a labelled diagram, a chart with a trend, a structure, a mechanism, a table of values). Logos, decorative photos, page furniture, and pictures with no testable content are not useful.
- kind: diagram | chart | table | photo | equation | map | decorative | other
- caption: a one-line caption in your own words (use the source's caption if one is visible).
- description: two to four sentences describing exactly what is shown, precise enough that cards can be written from it.
- parts: every labelled part, axis, series, or region, as short strings ("A: mitochondrion", "x-axis: time (s)").
- facts: testable facts the figure conveys, grounded in what is visible and the surrounding text.
- suggested_cards: how many good cards the figure supports (0-8).
Return only JSON."""


def vision_messages(image_bytes, mime, context_text):
    context = (context_text or "")[:6000]
    return [
        {"role": "system", "content": VISION_SYSTEM},
        {
            "role": "user",
            "content": [
                text_part(f"Nearby source text:\n{context or '(none)'}\n\nAnalyse this figure."),
                image_part(image_bytes, mime=mime, detail="high"),
            ],
        },
    ]


def analyze_figure(client, image_bytes, mime, context_text):
    """Pure: returns the analysis dict plus usage."""
    result = client.chat("vision", vision_messages(image_bytes, mime, context_text), response_format=VISION_SCHEMA, max_tokens=3000)
    try:
        data = extract_json(result.content)
    except Exception:
        data = {"useful": False, "kind": "other", "caption": "", "description": "", "parts": [], "facts": [], "suggested_cards": 0}
    data["usage"] = dict(result.usage)
    data["model"] = result.model
    return data
