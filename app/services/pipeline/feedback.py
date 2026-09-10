"""Close the loop with Anki.

The export stamps every note with a stable guid. When the student later exports their
studied deck (or collection) back, we read the SQLite collection inside the package,
match notes by guid, and attach real review statistics to our cards. Cards with high
lapse counts or "again" rates are *struggling*; the coach pass sends them to the model
with their stats and the source unit and rewrites or splits them.
"""

import io
import logging
import sqlite3
import tempfile
import zipfile
from collections import defaultdict

from flask import current_app

from ...extensions import db
from ...models import Card, Deck, LLMRun, PipelineTask, Source, utcnow
from ..llm import extract_json, is_terminal_error
from ..validators import is_valid_cloze, normalize_math, normalize_text
from . import critic as critic_mod
from .routing import LLMClient

logger = logging.getLogger(__name__)

STRUGGLE_MIN_REPS = 3
STRUGGLE_LAPSES = 2
STRUGGLE_AGAIN_RATE = 0.4


class ImportError_(RuntimeError):
    pass


def _open_collection_bytes(package_bytes):
    """Return raw SQLite bytes from an .apkg/.colpkg, decompressing zstd if needed."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(package_bytes))
    except zipfile.BadZipFile as exc:
        raise ImportError_("That file is not an Anki package (.apkg/.colpkg).") from exc
    names = set(zf.namelist())
    if "collection.anki21b" in names:
        raw = zf.read("collection.anki21b")
        try:
            import zstandard
        except ImportError as exc:
            raise ImportError_(
                "This package uses Anki's newer compressed format. Either install the `zstandard` "
                "package on the server, or re-export from Anki with 'Support older Anki versions' ticked."
            ) from exc
        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    for name in ("collection.anki21", "collection.anki2"):
        if name in names:
            return zf.read(name)
    raise ImportError_("No collection database found inside the package.")


def read_review_stats(package_bytes):
    """Parse a package into {guid: stats}. Stats are aggregated over all cards of a note."""
    sqlite_bytes = _open_collection_bytes(package_bytes)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp.write(sqlite_bytes)
        path = tmp.name
    stats = {}
    try:
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, guid FROM notes")
            note_guid = {nid: guid for nid, guid in cur.fetchall()}
            cur.execute("SELECT id, nid, ivl, factor, reps, lapses FROM cards")
            card_note = {}
            per_note = defaultdict(lambda: {"reps": 0, "lapses": 0, "ivl": 0, "factor": 0, "cards": 0, "again": 0, "reviews": 0})
            for cid, nid, ivl, factor, reps, lapses in cur.fetchall():
                card_note[cid] = nid
                agg = per_note[nid]
                agg["reps"] += int(reps or 0)
                agg["lapses"] += int(lapses or 0)
                agg["ivl"] = max(agg["ivl"], int(ivl or 0))
                agg["factor"] = max(agg["factor"], int(factor or 0))
                agg["cards"] += 1
            try:
                cur.execute("SELECT cid, ease FROM revlog")
                for cid, ease in cur.fetchall():
                    nid = card_note.get(cid)
                    if nid is None:
                        continue
                    per_note[nid]["reviews"] += 1
                    if int(ease or 0) == 1:
                        per_note[nid]["again"] += 1
            except sqlite3.DatabaseError:
                pass
            for nid, agg in per_note.items():
                guid = note_guid.get(nid)
                if not guid:
                    continue
                reviews = agg["reviews"] or agg["reps"]
                again_rate = (agg["again"] / reviews) if reviews else 0.0
                stats[guid] = {
                    "reps": agg["reps"], "lapses": agg["lapses"], "interval_days": agg["ivl"],
                    "ease": (agg["factor"] / 10.0) if agg["factor"] else None, "again_rate": round(again_rate, 3),
                    "reviews": reviews,
                }
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise ImportError_("The package's collection database could not be read.") from exc
    finally:
        try:
            import os

            os.remove(path)
        except OSError:
            pass
    return stats


def is_struggling(stats):
    if not stats:
        return False
    reps = int(stats.get("reps") or 0)
    if reps < STRUGGLE_MIN_REPS:
        return False
    return int(stats.get("lapses") or 0) >= STRUGGLE_LAPSES or float(stats.get("again_rate") or 0) >= STRUGGLE_AGAIN_RATE


def apply_review_stats(deck_id, stats_by_guid):
    """Attach stats to matching cards. Returns (matched, struggling)."""
    matched = struggling = 0
    for card in Card.query.filter_by(deck_id=deck_id).filter(Card.guid.isnot(None)).all():
        stats = stats_by_guid.get(card.guid)
        if not stats:
            continue
        matched += 1
        stats = dict(stats)
        stats["struggling"] = is_struggling(stats)
        stats["imported_at"] = utcnow().isoformat()
        card.review_stats_json = stats
        tags = [t for t in (card.tags or []) if t != "struggling"]
        if stats["struggling"]:
            struggling += 1
            tags.append("struggling")
        card.tags = tags
    db.session.commit()
    return matched, struggling


def coach_cards(deck_id, card_ids=None):
    """Diagnose + rewrite struggling cards. Returns a summary dict."""
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return None
    query = Card.query.filter_by(deck_id=deck_id).filter(Card.status.in_(("ok", "needs_review")))
    if card_ids:
        query = query.filter(Card.id.in_(list(card_ids)))
    else:
        query = query.filter(Card.review_stats_json.isnot(None))
    cards = [c for c in query.all() if (card_ids or (c.review_stats_json or {}).get("struggling"))]
    if not cards:
        return {"cards": 0, "rewritten": 0, "split": 0, "kept": 0}
    client = LLMClient(current_app.config)
    phase = PipelineTask(deck_id=deck_id, seq=0, phase="coach", kind="phase", label="Coach struggling cards",
                         status="running", started_at=utcnow())
    db.session.add(phase)
    db.session.commit()
    by_source = defaultdict(list)
    for c in cards:
        by_source[c.source_id].append(c)
    rewritten = split = kept = 0
    for source_id, group in by_source.items():
        source = db.session.get(Source, source_id) if source_id else None
        source_text = source.text if source else deck.source_text[:20000]
        for start in range(0, len(group), 12):
            batch = group[start : start + 12]
            node = PipelineTask(deck_id=deck_id, parent_id=phase.id, seq=start + 1, phase="coach", kind="task",
                                label=f"Diagnose {len(batch)} cards · {(source.title if source else 'deck')[:80]}",
                                model=client.model_for("critic"), status="running", started_at=utcnow(),
                                unit_ids=[source.idx] if source else [])
            db.session.add(node)
            db.session.commit()
            payload = [(_card_dict(c), c.review_stats_json or {}) for c in batch]
            messages = critic_mod.diagnose_messages(payload, source_text)
            try:
                result = client.chat("critic", messages, response_format=critic_mod.DIAGNOSE_SCHEMA, max_tokens=8000)
                data = extract_json(result.content)
            except Exception as exc:
                node.status = "failed"
                node.error = str(exc)[:500]
                node.finished_at = utcnow()
                db.session.commit()
                if is_terminal_error(exc):
                    phase.status = "failed"
                    db.session.commit()
                    raise
                continue
            db.session.add(LLMRun(deck_id=deck_id, task_id=node.id, role="critic", model=result.model,
                                  prompt_version="coach-v1", input_tokens=result.input_tokens,
                                  output_tokens=result.output_tokens, cost_estimate=result.cost,
                                  response_text=result.content, parsed_json=data))
            for item in data.get("cards") or []:
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                if not (0 <= idx < len(batch)):
                    continue
                card = batch[idx]
                action = item.get("action") or "keep"
                replacements = [r for r in (item.get("replacements") or []) if _valid_replacement(r)]
                diagnosis = (item.get("diagnosis") or "")[:600]
                card.critic_json = {**(card.critic_json or {}), "coach": {"action": action, "diagnosis": diagnosis}}
                if action == "keep" or not replacements:
                    kept += 1
                    continue
                if action == "rewrite" and len(replacements) == 1:
                    _apply_replacement(card, replacements[0])
                    card.status = "needs_review"
                    card.tags = _add_tags(card.tags, ["coach:rewritten"])
                    rewritten += 1
                    continue
                # split (or a rewrite that produced several cards)
                card.status = "deleted"
                card.tags = _add_tags(card.tags, ["coach:split"])
                for r in replacements[:4]:
                    new = Card(deck_id=deck_id, source_id=card.source_id, task_id=node.id, type=r["type"], status="needs_review",
                               strategy=card.strategy, order_key=card.order_key, source_quote=card.source_quote,
                               tags=_add_tags([t for t in (card.tags or []) if not t.startswith("coach:") and t != "struggling"], ["coach:split_child"]))
                    _apply_replacement(new, r)
                    db.session.add(new)
                split += 1
            node.status = "done"
            node.finished_at = utcnow()
            node.cards_made = len(batch)
            node.input_tokens = result.input_tokens
            node.output_tokens = result.output_tokens
            node.cost = result.cost
            db.session.commit()
    phase.status = "done"
    phase.finished_at = utcnow()
    db.session.commit()
    return {"cards": len(cards), "rewritten": rewritten, "split": split, "kept": kept}


def _card_dict(card):
    return {"type": card.type, "front": card.front, "back": card.back, "cloze_text": card.cloze_text,
            "extra": card.extra, "source_quote": card.source_quote}


def _valid_replacement(r):
    if not isinstance(r, dict):
        return False
    if r.get("type") == "basic":
        return bool(r.get("front") and r.get("back"))
    if r.get("type") == "cloze":
        return bool(r.get("cloze_text")) and is_valid_cloze(r["cloze_text"])
    return False


def _apply_replacement(card, r):
    card.type = r["type"]
    if r["type"] == "basic":
        card.front = normalize_math(normalize_text(r["front"]))
        card.back = normalize_math(normalize_text(r["back"]))
        card.cloze_text = None
        card.extra = None
    else:
        card.cloze_text = normalize_math(normalize_text(r["cloze_text"]))
        card.extra = normalize_math(normalize_text(r.get("extra") or ""))
        card.front = None
        card.back = None


def _add_tags(tags, new):
    out = list(tags or [])
    for t in new:
        if t not in out:
            out.append(t)
    return out
