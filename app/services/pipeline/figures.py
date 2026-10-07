"""Figures — pull images out of an uploaded PDF and let the vision model read them.

Figure regions are rendered from the page (not just the embedded bitmap) so vector
labels drawn over a raster image survive. Each figure then gets one vision call that
says whether it is material to learn, what kind of figure it is, which labelled parts
and facts it conveys, and what it adds beyond the text around it. The planner decides
from that analysis which figures get `figure_recall` tasks; their images ship inside the
.apkg.

A figure that is only text set as an image (a table of formulas, a block of rules) is
not treated as a picture: the vision pass transcribes it and the transcript joins the
text of its unit, so it is planned, written and de-duplicated like any other text.
"""

import hashlib
import logging

from ..chunking import clean_text
from ..llm import extract_json, image_part, json_schema_format, text_part

logger = logging.getLogger(__name__)

VISION_PROMPT_VERSION = "vision-v2"
MIN_SIDE_PT = 110  # skip icons, bullets, rules
MAX_PAGE_FRACTION = 0.92  # skip full-page scans/backgrounds
RENDER_DPI = 120
MAX_RENDER_SIDE = 1400
# Cards a figure may be given when the planner did not rule on it. The vision pass's
# count is advice; this only stops one over-generous analysis from flooding a deck.
MAX_ADVISED_CARDS = 8
MAX_TRANSCRIPT_CHARS = 4000


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
            "text_only": {"type": "boolean"},
            "transcript": {"type": "string"},
            "adds": {"type": "string"},
            "suggested_cards": {"type": "integer"},
        },
        "required": ["useful", "kind", "caption", "description", "parts", "facts", "text_only", "transcript", "adds",
                     "suggested_cards"],
    },
)

VISION_SYSTEM = """You analyse a figure from study material for a flashcard generator. Nearby source text is provided for context.

Decide `useful`: true only if the figure is material a student has to learn and could be examined on (a labelled diagram, a chart with a trend, a structure, a mechanism, a table or formula that is part of the content). Not useful: logos, decorative photos, page furniture, pictures with no testable content, and practice material. Exercise and quiz questions, homework prompts and answer keys are something to do, not something to learn.
- kind: diagram | chart | table | photo | equation | map | decorative | other
- caption: a one-line caption in your own words (use the source's caption if one is visible).
- description: two to four sentences describing exactly what is shown, precise enough that cards can be written from it.
- parts: every labelled part, axis, series, or region, as short strings ("A: mitochondrion", "x-axis: time (s)").
- facts: what a student should take away from the figure, as statements that stay true beyond this one picture: what a notation or symbol means, how a structure is organised, what a part does, the trend a chart shows, a rule a table states. Ground each in what is visible. Leave out the incidental details of a single example (the particular numbers in a worked problem, which state one arrow of a sample diagram leads to) unless the source expects the student to know this exact figure. Do not copy in facts from the nearby text that the figure itself does not show.
- text_only: true when the figure is only text set as an image (prose, a list, a table of text or formulas, equations, grammar rules) with no drawing whose layout carries meaning.
- transcript: for a text_only figure, its full content as plain text, line by line, exactly as written (math as \\( ... \\)). An empty string for every other figure.
- adds: one sentence on what the figure gives a student beyond the nearby source text. If the nearby text already states everything the figure shows, say so.
- suggested_cards: how many cards the figure is worth on top of the cards the text gets anyway. Count one for each fact above that the nearby text does not already state. Most figures are worth 0 to 2; go higher (8 at most) only for a figure dense with content found nowhere in the text. 0 is the right answer for a useful figure whose content the text already covers, and for a text_only figure, whose transcript is carded as text.
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


def number_figures(figures):
    """Figure.id -> the number a deck's figure goes by ("Figure 3"), in page order. A
    deck's figures are fixed at upload, so the numbers hold across runs."""
    ordered = sorted(figures, key=lambda fig: (fig.page or 0, fig.id))
    return {fig.id: number for number, fig in enumerate(ordered, start=1)}


def describe_figure(figure):
    """What the vision pass read off a figure, as prompt text. `figure` is
    {caption, description, parts, facts, adds} with parts and facts already joined."""
    text = (
        f"Caption: {figure.get('caption') or '(none)'}\n"
        f"Description: {figure.get('description') or '(none)'}\n"
        f"Labelled parts / data: {figure.get('parts') or '(none)'}\n"
        f"Testable facts the figure conveys: {figure.get('facts') or '(none)'}"
    )
    if figure.get("adds"):
        text += f"\nWhat it adds beyond the text: {figure['adds']}"
    return text


def advised_cards(analysis):
    """Cards the vision pass thinks a figure is worth on top of its unit's text cards."""
    try:
        suggested = int((analysis or {}).get("suggested_cards") or 0)
    except (TypeError, ValueError):
        suggested = 0
    return max(0, min(MAX_ADVISED_CARDS, suggested))


def transcript_of(analysis):
    """The transcript of a figure that is only text set as an image, or "". Such a figure
    is source text, not a picture: shown on a card it would print the answer above the
    question, so its content is carded from the transcript instead."""
    analysis = analysis or {}
    if not analysis.get("text_only"):
        return ""
    return clean_text(str(analysis.get("transcript") or ""))[:MAX_TRANSCRIPT_CHARS]


def transcript_block(page, caption, transcript):
    """A transcript as it is appended to the text of the figure's unit."""
    where = f"an image on p.{page}" if page else "an image"
    title = " ".join((caption or "").split()).rstrip(".")
    return f"Text from {where}{f' ({title})' if title else ''}:\n{transcript}"


def analyze_figure(client, image_bytes, mime, context_text):
    """Pure: returns the analysis dict plus usage."""
    result = client.chat("vision", vision_messages(image_bytes, mime, context_text), response_format=VISION_SCHEMA, max_tokens=4000)
    try:
        data = extract_json(result.content)
    except Exception:
        data = {"useful": False, "kind": "other", "caption": "", "description": "", "parts": [], "facts": [],
                "text_only": False, "transcript": "", "adds": "", "suggested_cards": 0}
    data["usage"] = dict(result.usage)
    data["model"] = result.model
    return data
