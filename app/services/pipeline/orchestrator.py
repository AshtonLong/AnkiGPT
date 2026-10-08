"""The generation run: map -> (figures) -> (cheat sheet) -> plan -> write -> critique ->
coverage -> reconcile -> finish. Figures are read before anything is planned, because
the planner decides which of them get cards and the cheat sheet keeps the diagrams.
Duplicates are resolved last, so the cards the coverage back-fill adds go through the
same de-duplication as the rest.

Everything model-facing is a pure job run through `parallel.run_jobs`; this module owns
the DB side: units, tasks, cards, the trace, and the deck's status transitions.

Non-destructive rule: nothing from a previous generation is deleted until the (cheap)
map + plan phases have succeeded. A quota/auth failure therefore never wipes a working
deck; a single bad worker only loses its own task.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from flask import current_app

from ...desktop import settings_page_name
from ...extensions import db
from ...models import Card, Deck, Figure, LLMRun, PipelineTask, Source
from ..chunking import clean_text, hash_text
from ..credentials import openrouter_key_for
from ..llm import OpenRouterConnectionError, OpenRouterError, TERMINAL_ERROR_MARKERS, is_terminal_error
from ..validators import is_math_valid, is_valid_cloze
from . import cheatsheet as cheatsheet_mod
from . import critic as critic_mod
from . import document_map as map_mod
from . import figures as figures_mod
from . import planner as planner_mod
from . import reconcile as reconcile_mod
from . import workers as workers_mod
from .cache import DBCache, NullCache, make_key
from .catalog import user_model
from .efforts import user_efforts
from .parallel import Job, run_jobs
from .routing import ChatResult, LLMClient
from .strategies import DEFAULT_STRATEGY, PROMPT_VERSION
from .trace import Tracer, phases_for

logger = logging.getLogger(__name__)

PHASE_WEIGHTS = {
    "map": 5, "figures": 5, "cheatsheet": 8, "plan": 10, "write": 42, "critique": 20, "coverage": 10, "reconcile": 5,
    "finish": 3,
}
# The coverage back-fill may add up to this share of the cards already written. It is a
# runaway guard, not a quota: what keeps the back-fill honest is the review each of its
# cards has to pass (`_review_backfill`).
BACKFILL_SHARE = 0.4


# ------------------------------------------------------------------ public entry
def generate_deck(deck_id, resume_from_plan=False):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return None
    try:
        return _run(deck, resume_from_plan=resume_from_plan)
    except Exception as exc:
        # Single guaranteed exit: ANY unhandled failure marks the deck failed so it
        # can never get stuck in "processing" forever.
        logger.exception("Deck %s generation failed", deck_id)
        _mark_failed(deck, format_generation_error(exc))
        return None


def format_generation_error(exc):
    if isinstance(exc, OpenRouterConnectionError):
        return "Couldn't reach OpenRouter. Check your internet connection, then retry."
    if isinstance(exc, OpenRouterError):
        status = exc.status_code
        detail = (exc.response_body or "").lower()
        if status == 429:
            if any(marker in detail for marker in TERMINAL_ERROR_MARKERS):
                return (
                    "Your OpenRouter credits ran out while processing this deck. "
                    "Top up at openrouter.ai, then retry."
                )
            return "OpenRouter rate limit was hit while processing this deck. Wait a minute and try again."
        if status in (401, 403):
            return (
                f"OpenRouter authentication failed. Check the API key under {settings_page_name()} "
                "and its model access."
            )
        if status == 400:
            return f"OpenRouter rejected a request: {exc.response_body or exc}"
        if status and status >= 500:
            return "OpenRouter is temporarily unavailable. Please try again shortly."
        return str(exc)
    return str(exc)


def _mark_failed(deck, message):
    try:
        db.session.rollback()
        run = dict(deck.run_json or {})
        run["last_error"] = message
        deck.run_json = run
        deck.status = "failed"
        db.session.commit()
    except Exception:
        logger.exception("Failed to mark deck %s as failed", deck.id)


# ------------------------------------------------------------------- run context
@dataclass
class RunContext:
    deck: Deck
    settings: dict
    client: LLMClient
    cache: DBCache
    tracer: Tracer
    max_workers: int
    units: list = field(default_factory=list)
    unit_by_idx: dict = field(default_factory=dict)
    source_ids: dict = field(default_factory=dict)  # unit idx -> Source.id
    plan: planner_mod.Plan = None
    doc_meta: dict = field(default_factory=dict)
    card_style: str = "basic"

    def units_for(self, idxs):
        return [self.unit_by_idx[i] for i in idxs if i in self.unit_by_idx]

    def source_text_for(self, idxs):
        return "\n\n".join(u.text for u in self.units_for(idxs))


def _build_context(deck):
    cfg = current_app.config
    settings = dict(deck.settings_json or {})
    settings.setdefault("card_style", deck.card_style)
    client = LLMClient(cfg, openrouter_key_for(deck.user), user_efforts(deck.user), user_model(deck.user))
    cache = DBCache(enabled=bool(cfg.get("PIPELINE_CACHE_ENABLED", True)))
    if not cfg.get("PIPELINE_CACHE_ENABLED", True):
        cache = NullCache()
    return RunContext(
        deck=deck,
        settings=settings,
        client=client,
        cache=cache,
        tracer=Tracer(deck),
        max_workers=int(cfg.get("PIPELINE_MAX_WORKERS", 6)),
        card_style=deck.card_style or "basic",
    )


def _abort_on(exc):
    return is_terminal_error(exc)


def _raise_if_terminal(results):
    for res in results.values():
        if res.error is not None and is_terminal_error(res.error):
            raise res.error


# --------------------------------------------------------------------- the run
def _run(deck, resume_from_plan=False):
    ctx = _build_context(deck)
    tracer = ctx.tracer
    deck.status = "processing"
    db.session.commit()

    stored_plan = (deck.run_json or {}).get("plan")
    resumed = bool(resume_from_plan and stored_plan)
    if resumed:
        _restore_plan(ctx)
        if "figure_skips" not in stored_plan:
            # Paused by a version whose planner did not decide the figures: read them if
            # that had not happened yet, and let each follow the vision pass.
            if "figures" not in tracer.phase_nodes:
                _phase_figures(ctx)
            planner_mod.settle_figures(ctx.units, ctx.plan, _plan_figures(ctx))
    else:
        tracer.clear()
        tracer.set_run(
            phase="map", last_error=None, plan=None, summary=None, cheat_sheet=None, started=True,
            prompt_version=PROMPT_VERSION, model=ctx.client.default_model,
        )
        _phase_map(ctx)
        # Before the sheet, which keeps the diagrams, and before the plan, which decides them.
        _phase_figures(ctx)
        if ctx.settings.get("cheat_sheet"):
            _phase_cheatsheet(ctx)
        _phase_plan(ctx)
        if ctx.settings.get("review_plan"):
            deck.status = "planned"
            tracer.set_run(phase="planned")
            db.session.commit()
            logger.info("Deck %s planned; waiting for review", deck.id)
            return deck.id

    written = _phase_write(ctx)
    _phase_critique(ctx, written)
    _phase_coverage(ctx)
    _phase_reconcile(ctx)
    _phase_finish(ctx)
    return deck.id


# ------------------------------------------------------------------------ map
def _phase_map(ctx):
    tracer = ctx.tracer
    deck = ctx.deck
    cfg = current_app.config
    tracer.phase("map")
    node = tracer.task("map", "Outline the document", model=ctx.client.model_for("mapper"))
    tracer.start(node)
    page_offsets = (deck.settings_json or {}).get("page_offsets") or (deck.run_json or {}).get("page_offsets")
    units, meta, result, messages = map_mod.build_document_map(
        ctx.client, deck.source_text, ctx.settings,
        max_unit_chars=int(cfg.get("PIPELINE_UNIT_MAX_CHARS", 14000)), page_offsets=page_offsets,
    )
    if not units:
        tracer.finish(node, status="failed", error="No usable text in the source.")
        raise OpenRouterError("The source contains no usable text.")
    if result is not None:
        tracer.log_call(node, "mapper", result, messages=messages, prompt_version=map_mod.MAP_PROMPT_VERSION,
                        parsed={"units": [u.to_dict() for u in units], **meta})
    ctx.units = units
    ctx.unit_by_idx = {u.idx: u for u in units}
    ctx.doc_meta = meta
    live = [u for u in units if not u.skipped]
    tracer.finish(
        node, cards_made=None,
        result={"units": len(units), "live_units": len(live), "candidates": meta.get("candidates"),
                "subject": meta.get("subject"), "mapped_by": meta.get("mapped_by")},
        usage=(result.usage if result is not None else None),
    )
    tracer.end_phase("map", result={"units": [u.to_dict() for u in units], **meta})


# ---------------------------------------------------------------- cheat sheet
def _phase_cheatsheet(ctx):
    """Swap every live unit's text for its exam cheat-sheet section. Runs before the plan
    (and before anything is persisted), so each later phase works from the cheat sheet
    alone and `_restore_plan` / regenerate pick it up from the Source rows for free."""
    tracer = ctx.tracer
    tracer.phase("cheatsheet")
    model = ctx.client.model_for("cheatsheet")
    live = [u for u in ctx.units if not u.skipped]
    figures = _sheet_figures(ctx)
    nodes = {}
    jobs = []
    for u in live:
        figs = figures.get(u.idx, [])
        messages = cheatsheet_mod.build_messages(u, ctx.units, ctx.settings, ctx.doc_meta, figures=figs)
        node = tracer.task("cheatsheet", (u.title or f"Unit {u.idx + 1}")[:150], unit_ids=[u.idx], model=model)
        nodes[node.id] = {"node": node, "unit": u, "messages": messages, "figures": figs}
        jobs.append(Job(
            id=node.id,
            fn=(lambda m=messages: cheatsheet_mod.write_cheat_sheet(ctx.client, m)),
            cache_key=make_key("cheatsheet", model, cheatsheet_mod.CHEATSHEET_PROMPT_VERSION, messages,
                               *ctx.client.effort_key("cheatsheet")),
            meta={"role": "cheatsheet", "model": model},
        ))

    stats = {"units": len(live), "condensed": 0, "emptied": 0, "failed": 0, "chars_in": 0, "chars_out": 0,
             "figures": 0, "kept_full": []}

    def on_start(job):
        tracer.start(nodes[job.id]["node"])

    def on_done(res):
        entry = nodes[res.job.id]
        node, unit, figs = entry["node"], entry["unit"], entry["figures"]
        if not res.ok:
            # A unit that could not be condensed keeps its full text rather than being lost.
            logger.warning("Deck %s cheat sheet for unit %s failed: %s", ctx.deck.id, unit.idx, res.error)
            stats["failed"] += 1
            stats["kept_full"].append(unit.idx)
            stats["figures"] += len(figs)
            unit.text = cheatsheet_mod.place_figures(unit.text, figs)
            tracer.log_call(node, "cheatsheet", None, messages=entry["messages"], error=res.error,
                            prompt_version=cheatsheet_mod.CHEATSHEET_PROMPT_VERSION)
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        value = res.value or {}
        sheet = cheatsheet_mod.place_figures(value.get("cheat_sheet") or "", figs)
        before = unit.chars
        cr = ChatResult(content=value.get("content") or "", message={}, usage=res.usage or {},
                        model=value.get("model") or node.model)
        tracer.log_call(node, "cheatsheet", cr, messages=entry["messages"],
                        prompt_version=cheatsheet_mod.CHEATSHEET_PROMPT_VERSION, parsed={"cheat_sheet": sheet},
                        cached=res.cached)
        if sheet:
            unit.text = sheet
            unit.density = max(int(unit.density or 3), cheatsheet_mod.CHEATSHEET_DENSITY)
            stats["condensed"] += 1
            stats["chars_in"] += before
            stats["chars_out"] += len(sheet)
            stats["figures"] += len(figs)
            node.label = f"{node.label} · {before:,} → {len(sheet):,} chars"
            if figs:
                node.label += f" · {len(figs)} diagram{'s' if len(figs) != 1 else ''}"
        else:
            unit.skipped = True
            unit.skip_reason = "Nothing exam-critical to put on the cheat sheet."
            stats["emptied"] += 1
            node.label = f"{node.label} · nothing exam-critical, skipped"
        tracer.finish(node, status="cached" if res.cached else "done",
                      result={"chars_in": before, "chars_out": len(sheet), "figures": len(figs)})

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    failed = stats["failed"]
    error = f"{failed} of {len(live)} units could not be condensed and were kept in full." if failed else None
    tracer.set_run(cheat_sheet=stats)
    tracer.end_phase("cheatsheet", status="failed" if failed else "done", error=error, result=stats)
    logger.info("Deck %s cheat sheet: %s units, %s -> %s chars, %s emptied, %s failed", ctx.deck.id, len(live),
                stats["chars_in"], stats["chars_out"], stats["emptied"], failed)
    if live and not any(not u.skipped for u in ctx.units):
        # Nothing has been wiped yet, so failing here keeps any previous deck intact.
        raise OpenRouterError(
            "The cheat sheet came back empty: nothing in this source looked exam-critical. "
            "Turn off the cheat sheet, or loosen the focus, and try again."
        )


# ----------------------------------------------------------------------- plan
def _phase_plan(ctx):
    tracer = ctx.tracer
    deck = ctx.deck
    cfg = current_app.config
    tracer.phase("plan")
    node = tracer.task("plan", "Plan the work order", model=ctx.client.model_for("planner"))
    tracer.start(node)
    figures = _plan_figures(ctx)
    budget = planner_mod.suggest_budget(ctx.units, planner_mod.requested_cards(ctx.settings), figures)
    events = []

    def on_event(kind, payload):
        events.append({"kind": kind, **payload})
        if len(events) % 3 == 0:
            node.result_json = {"events": events[-40:]}
            db.session.commit()

    plan = None
    calls = []
    try:
        plan, transcript, calls = planner_mod.plan_with_agent(
            ctx.client, ctx.units, ctx.settings, ctx.doc_meta, budget,
            max_turns=int(cfg.get("PIPELINE_PLANNER_MAX_TURNS", 14)),
            figures=figures, on_event=on_event,
        )
    except Exception as exc:
        if is_terminal_error(exc):
            tracer.finish(node, status="failed", error=exc)
            raise
        logger.warning("Planner failed (%s); using heuristic plan", exc)
        tracer.log_call(node, "planner", None, error=exc, prompt_version=planner_mod.PLAN_PROMPT_VERSION)
        plan = planner_mod.plan_heuristic(ctx.units, budget, figures)
    for i, call in enumerate(calls):
        tracer.log_call(
            node, "planner", call, prompt_version=planner_mod.PLAN_PROMPT_VERSION,
            parsed=(plan.to_dict() if i == len(calls) - 1 else None),
        )
    ctx.plan = plan
    for idx, reason in plan.skips.items():
        unit = ctx.unit_by_idx.get(idx)
        if unit is not None:
            unit.skipped = True
            unit.skip_reason = unit.skip_reason or reason
    if ctx.settings.get("cheat_sheet"):
        _drop_uncarded_diagrams(ctx, plan, figures)

    # ---- Map + plan succeeded: now it is safe to replace the previous generation.
    _wipe_previous_generation(deck)
    _persist_units(ctx)
    tracer.finish(
        node,
        result={
            "events": events[-40:], "tasks": len(plan.tasks), "skips": len(plan.skips), "budget": budget,
            "figure_tasks": len(plan.figure_tasks), "figure_skips": len(plan.figure_skips),
            "turns": plan.turns, "mode": plan.mode, "summary": plan.summary,
        },
    )
    tracer.set_run(plan=plan.to_dict(), doc_meta=ctx.doc_meta, budget=budget, summary=plan.summary)
    tracer.end_phase("plan", result={"tasks": len(plan.tasks), "summary": plan.summary, "mode": plan.mode})


def _drop_uncarded_diagrams(ctx, plan, figures):
    """The sheet keeps the diagrams that earn cards. It was written before the plan, with
    every picture on it, so the ones the planner gave no cards come off again. A section
    that was nothing but such a diagram has nothing left to write from and is skipped."""
    dropped = {f["number"] for f in figures if f["id"] in plan.figure_skips}
    if not dropped:
        return
    for unit in ctx.units:
        if unit.skipped:
            continue
        unit.text = cheatsheet_mod.remove_figures(unit.text, dropped)
        if not unit.text:
            unit.skipped = True
            unit.skip_reason = plan.skips[unit.idx] = "Nothing exam-critical to put on the cheat sheet."
    plan.tasks[:] = [t for t in plan.tasks if any(i not in plan.skips for i in t.unit_idxs)]
    stats = dict(ctx.tracer.get_run().get("cheat_sheet") or {})
    stats["figures"] = max(0, int(stats.get("figures") or 0) - len(dropped))
    ctx.tracer.set_run(cheat_sheet=stats)


def _wipe_previous_generation(deck):
    Card.query.filter_by(deck_id=deck.id).delete()
    Source.query.filter_by(deck_id=deck.id).delete()
    # Old PipelineTask rows were cleared at run start, which nulled their LLMRun.task_id;
    # runs from *this* run are attached to live tasks and survive.
    LLMRun.query.filter_by(deck_id=deck.id, task_id=None).delete()
    for fig in Figure.query.filter_by(deck_id=deck.id).all():
        fig.source_id = None
    db.session.commit()


def _persist_units(ctx):
    rows = []
    for u in ctx.units:
        rows.append(
            Source(
                deck_id=ctx.deck.id, idx=u.idx, title=(u.title or f"Unit {u.idx + 1}")[:200], text=u.text,
                hash=hash_text(u.text), kind=u.kind, density=u.density, char_start=u.char_start,
                char_end=u.char_end, page_start=u.page_start, page_end=u.page_end,
                depends_on=list(u.depends_on or []), skipped=bool(u.skipped), skip_reason=u.skip_reason,
                summary=u.summary,
            )
        )
    db.session.add_all(rows)
    db.session.commit()
    ctx.source_ids = {r.idx: r.id for r in rows}
    # Attach figures to the unit whose page range contains them.
    for fig in Figure.query.filter_by(deck_id=ctx.deck.id).all():
        idx = _unit_idx_for_page(ctx, fig.page)
        fig.source_id = ctx.source_ids.get(idx) if idx is not None else None
    db.session.commit()


def _restore_plan(ctx):
    """Resume after a 'planned' pause: rebuild units from Source rows and the plan from run_json."""
    deck = ctx.deck
    run = dict(deck.run_json or {})
    sources = Source.query.filter_by(deck_id=deck.id).order_by(Source.idx).all()
    units = []
    for s in sources:
        unit = map_mod.Unit(
            idx=s.idx, title=s.title, text=s.text, char_start=s.char_start, char_end=s.char_end, kind=s.kind or "prose",
            density=s.density or 3, depends_on=list(s.depends_on or []), skipped=bool(s.skipped),
            skip_reason=s.skip_reason, summary=s.summary, page_start=s.page_start, page_end=s.page_end,
        )
        units.append(unit)
    ctx.units = units
    ctx.unit_by_idx = {u.idx: u for u in units}
    ctx.source_ids = {s.idx: s.id for s in sources}
    ctx.doc_meta = run.get("doc_meta") or {}
    ctx.plan = planner_mod.Plan.from_dict(run.get("plan") or {})
    # Re-seed the tracer sequence after the existing map/plan nodes.
    last = PipelineTask.query.filter_by(deck_id=deck.id).order_by(PipelineTask.seq.desc()).first()
    ctx.tracer._seq = last.seq if last else 0
    for node in PipelineTask.query.filter_by(deck_id=deck.id, kind="phase").all():
        ctx.tracer.phase_nodes[node.phase] = node
    # Cards from a previous completed run of this plan (re-run) must not linger.
    Card.query.filter_by(deck_id=deck.id).delete()
    db.session.commit()
    ctx.tracer.set_run(phase="write", last_error=None)


def _unit_idx_for_page(ctx, page):
    """The unit a page belongs to: the one whose page range holds it. A page with a
    figure and no text sits between two ranges; it continues the unit that was being read
    when it came up, which is the last one to start at or before it."""
    if page is None:
        return None
    paged = [u for u in ctx.units if u.page_start]
    for u in paged:
        if u.page_end and u.page_start <= page <= u.page_end:
            return u.idx
    before = [u for u in paged if u.page_start <= page]
    if before:
        return max(before, key=lambda u: (u.page_start, u.idx)).idx
    return paged[0].idx if paged else None


# -------------------------------------------------------------------- figures
def _figures_wanted(ctx):
    return bool(current_app.config.get("PIPELINE_FIGURES_ENABLED", True) and ctx.settings.get("use_figures", True))


def _figure_payload(fig):
    analysis = fig.analysis_json or {}
    return {
        "caption": fig.caption, "description": fig.description,
        "parts": "; ".join(analysis.get("parts") or []), "facts": "; ".join(analysis.get("facts") or []),
        "adds": analysis.get("adds"),
    }


def _figure_label(fig):
    caption = " ".join((fig.caption or "").split())
    return f"Figure on p.{fig.page}" + (f": {caption}" if caption else "")


def _pictures(ctx):
    """[(number, Figure, unit idx)] for the figures that stay pictures, in page order:
    the vision pass judged them material to learn, they sit in a live unit, and they are
    not just text set as an image (that kind joined its unit's text as a transcript)."""
    if not _figures_wanted(ctx):
        return []
    figs = Figure.query.filter_by(deck_id=ctx.deck.id).all()
    numbers = figures_mod.number_figures(figs)
    pictures = []
    for fig in sorted(figs, key=lambda f: numbers[f.id]):
        idx = _unit_idx_for_page(ctx, fig.page)
        unit = ctx.unit_by_idx.get(idx) if idx is not None else None
        if not fig.useful or unit is None or unit.skipped or figures_mod.transcript_of(fig.analysis_json):
            continue
        pictures.append((numbers[fig.id], fig, idx))
    return pictures


def _plan_figures(ctx):
    """The figures the planner rules on, as it is shown them."""
    return [
        {"id": fig.id, "number": number, "unit_idx": idx, "page": fig.page, "kind": fig.kind, "caption": fig.caption,
         "adds": (fig.analysis_json or {}).get("adds"), "suggested": figures_mod.advised_cards(fig.analysis_json)}
        for number, fig, idx in _pictures(ctx)
    ]


def _sheet_figures(ctx):
    """unit idx -> the diagrams its cheat-sheet section is written with: every picture.
    The ones the planner then gives no cards come off again (`_drop_uncarded_diagrams`)."""
    by_unit = defaultdict(list)
    for number, fig, idx in _pictures(ctx):
        analysis = fig.analysis_json or {}
        by_unit[idx].append({
            "number": number, "page": fig.page, "kind": fig.kind, "caption": fig.caption,
            "description": fig.description, "parts": analysis.get("parts") or [], "facts": analysis.get("facts") or [],
        })
    return by_unit


def _fold_in_transcripts(ctx):
    """A figure that is only text set as an image is source text, so its transcript joins
    the text of its unit and is condensed, planned and carded with it. Returns how many
    figures went that way."""
    folded = 0
    for fig in Figure.query.filter_by(deck_id=ctx.deck.id).order_by(Figure.page, Figure.id).all():
        idx = _unit_idx_for_page(ctx, fig.page)
        unit = ctx.unit_by_idx.get(idx) if idx is not None else None
        transcript = figures_mod.transcript_of(fig.analysis_json) if fig.useful else ""
        if not transcript or unit is None or unit.skipped:
            continue
        unit.text = f"{unit.text}\n\n{figures_mod.transcript_block(fig.page, fig.caption, transcript)}"
        folded += 1
    return folded


def _phase_figures(ctx):
    """Read every figure with the vision model. Nothing is decided here: the analyses
    are what the cheat sheet and the planner work from."""
    tracer = ctx.tracer
    figs = Figure.query.filter_by(deck_id=ctx.deck.id).order_by(Figure.page).all()
    if not figs or not _figures_wanted(ctx):
        tracer.phase("figures", status="skipped")
        tracer.end_phase("figures", status="skipped")
        return
    tracer.phase("figures")
    nodes = {}
    jobs = []
    for fig in figs:
        idx = _unit_idx_for_page(ctx, fig.page)
        unit = ctx.unit_by_idx.get(idx) if idx is not None else None
        if unit is None or unit.skipped:
            # Nothing is written from a skipped unit, so its figures are not worth a call.
            continue
        context = unit.text
        node = tracer.task("figures", f"Read figure on p.{fig.page}", strategy="vision", unit_ids=[idx],
                           model=ctx.client.model_for("vision"))
        nodes[fig.id] = node
        key = make_key("vision", ctx.client.model_for("vision"), figures_mod.VISION_PROMPT_VERSION, fig.hash, context[:6000],
                       *ctx.client.effort_key("vision"))
        # Tracer commits expire ORM attributes. Resolve image data here, while
        # the DB session is available; pool threads must never load Figure rows.
        image_bytes, mime = bytes(fig.image), fig.mime or "image/png"
        jobs.append(Job(
            id=fig.id,
            fn=(lambda image=image_bytes, m=mime, c=context: figures_mod.analyze_figure(ctx.client, image, m, c)),
            cache_key=key, meta={"role": "vision", "model": ctx.client.model_for("vision")},
        ))

    def on_start(job):
        tracer.start(nodes[job.id])

    def on_done(res):
        node = nodes[res.job.id]
        fig = db.session.get(Figure, res.job.id)
        if not res.ok:
            logger.warning("Deck %s figure %s analysis failed: %s", ctx.deck.id, fig.id, res.error)
            tracer.finish(node, status="failed", error=res.error)
            return
        data = res.value or {}
        fig.useful = bool(data.get("useful"))
        fig.kind = data.get("kind")
        fig.caption = (data.get("caption") or "")[:500]
        fig.description = (data.get("description") or "")[:2000]
        fig.analysis_json = {k: data.get(k) for k in ("parts", "facts", "suggested_cards", "text_only", "transcript", "adds")}
        db.session.commit()
        tracer.finish(
            node, status="cached" if res.cached else "done", usage=res.usage, cached=res.cached,
            result={"useful": fig.useful, "kind": fig.kind, "caption": fig.caption,
                    "text_only": bool(figures_mod.transcript_of(fig.analysis_json))},
        )

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    failed = sum(1 for res in results.values() if not res.ok)
    useful = sum(1 for res in results.values() if res.ok and (res.value or {}).get("useful"))
    transcribed = _fold_in_transcripts(ctx)
    pictures = len(_pictures(ctx))
    error = f"{failed} of {len(jobs)} figures could not be analyzed. See the failed figure tasks for details." if failed else None
    tracer.end_phase("figures", status="failed" if failed else "done", error=error,
                     result={"figures": len(figs), "read": len(jobs), "useful": useful, "pictures": pictures,
                             "transcribed": transcribed, "failed": failed})
    logger.info("Deck %s figures: %s found, %s read, %s useful (%s pictures, %s transcribed), %s failed",
                ctx.deck.id, len(figs), len(jobs), useful, pictures, transcribed, failed)


# ---------------------------------------------------------------------- write
def _squash(text):
    return " ".join((text or "").lower().split())


def _card_unit_idx(ctx, task, card):
    """The unit a card is filed under. A task may read several units; each of its cards
    belongs to the unit its source quote comes from, so that every unit's cards can be
    found, ordered and audited as its own. Without a quote that places it, the first."""
    idxs = list(task.unit_idxs or [])
    if not idxs:
        return None
    quote = _squash(card.get("source_quote"))
    if len(idxs) > 1 and quote:
        for idx in idxs:
            unit = ctx.unit_by_idx.get(idx)
            if unit is not None and quote in _squash(unit.text):
                return idx
    return idxs[0]


def _persist_cards(ctx, node, task, raw_cards, origin_tag=None):
    """Validate + store worker output. Returns (rows, auto_deleted)."""
    from ..schemas import CardSchema

    rows = []
    auto_deleted = 0
    for raw in raw_cards:
        try:
            model = CardSchema.model_validate(raw)
        except Exception:
            continue
        card = workers_mod.normalize_card(model, strategy=task.strategy)
        unit_idx = _card_unit_idx(ctx, task, card)
        issues = []
        content = " ".join(filter(None, [card.get("front"), card.get("back"), card.get("cloze_text"), card.get("extra")]))
        if card["type"] == "cloze" and not is_valid_cloze(card["cloze_text"]):
            issues.append("invalid_cloze")
        if not is_math_valid(content):
            issues.append("invalid_math")
        tags = list(card["tags"])
        tags.append(f"strategy:{task.strategy}")
        if unit_idx is not None:
            tags.append(f"unit:{unit_idx + 1}")
        if origin_tag:
            tags.append(origin_tag)
        status = "ok"
        if issues:
            status = "deleted"
            auto_deleted += 1
            tags.append("auto_deleted")
            tags.extend(f"validation:{i}" for i in issues)
        rows.append(
            Card(
                deck_id=ctx.deck.id, source_id=ctx.source_ids.get(unit_idx), task_id=node.id, figure_id=task.figure_id,
                type=card["type"], front=card.get("front"), back=card.get("back"), cloze_text=card.get("cloze_text"),
                extra=card.get("extra"), tags=_dedupe_tags(tags), status=status, strategy=task.strategy,
                source_quote=card.get("source_quote"),
            )
        )
    db.session.add_all(rows)
    db.session.commit()
    return rows, auto_deleted


def _dedupe_tags(tags):
    out = []
    seen = set()
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _cards_by_unit(ctx):
    """unit idx -> the deck's ok cards from that unit: the cards filed under it plus the
    cards of every task that read it. A card of a multi-unit task whose quote could not be
    placed is filed under the task's first unit, and still has to count for the others."""
    task_units = {
        t.id: list(t.unit_ids or [])
        for t in PipelineTask.query.filter_by(deck_id=ctx.deck.id, kind="task").all() if t.phase in ("write", "coverage")
    }
    idx_of = {sid: idx for idx, sid in ctx.source_ids.items()}
    by_unit = defaultdict(list)
    for card in Card.query.filter_by(deck_id=ctx.deck.id, status="ok").order_by(Card.id).all():
        for idx in sorted({idx_of.get(card.source_id), *task_units.get(card.task_id, [])} - {None}):
            by_unit[idx].append(card)
    return by_unit


def _card_lines(cards):
    return [critic_mod.card_line(_card_dict(c)) for c in cards]


def _worker_job_for(ctx, task, node, siblings, figure_payload=None, existing=None):
    units = ctx.units_for(task.unit_idxs)
    messages = workers_mod.build_worker_messages(task, units, ctx.settings, ctx.card_style, siblings=siblings,
                                                figure=figure_payload, existing=existing)
    model = ctx.client.model_for("worker")
    key = make_key("worker", model, workers_mod.WORKER_PROMPT_VERSION, messages, *ctx.client.effort_key("worker"))
    return Job(
        id=node.id,
        fn=(lambda m=messages, t=task: {**workers_mod.run_worker(ctx.client, m, t.target_cards), "messages": m}),
        cache_key=key, meta={"role": "worker", "model": model, "task": task},
    ), messages


def _run_write_tasks(ctx, phase, tasks, origin_tag=None, after_existing=False):
    """Shared by the write phase and the coverage gap-fill round. With `after_existing`
    each worker is shown the cards the deck already has from its units, so a task that
    writes after others (a figure, a coverage gap) adds to them instead of repeating them.
    Returns {node_id: {"task": PlanTask, "node": node, "cards": [Card rows]}}."""
    tracer = ctx.tracer
    nodes = {}
    jobs = []
    figures = {t.figure_id: db.session.get(Figure, t.figure_id) for t in tasks if t.figure_id}
    existing_by_unit = _cards_by_unit(ctx) if after_existing else {}

    def brief(task):
        fig = figures.get(task.figure_id)
        return " ".join(filter(None, [f"{_figure_label(fig)}." if fig is not None else "", task.notes]))

    by_unit = defaultdict(list)
    for t in tasks:
        for i in t.unit_idxs:
            by_unit[i].append(t)
    for task in tasks:
        units = ctx.units_for(task.unit_idxs)
        fig = figures.get(task.figure_id)
        title = _figure_label(fig) if fig is not None else ", ".join(u.title for u in units)
        node = tracer.task(
            phase, title[:150] or f"units {task.unit_idxs}", strategy=task.strategy, unit_ids=list(task.unit_idxs),
            model=ctx.client.model_for("worker"), target_cards=task.target_cards, notes=task.notes,
        )
        siblings = []
        for i in task.unit_idxs:
            for other in by_unit[i]:
                hint = (other.strategy, tuple(other.unit_idxs), brief(other))
                if other is not task and hint not in siblings:
                    siblings.append(hint)
        existing = None
        if after_existing:
            lines = {}
            for i in task.unit_idxs:
                for card in existing_by_unit.get(i, []):
                    lines.setdefault(card.id, critic_mod.card_line(_card_dict(card)))
            existing = list(lines.values())
        job, _messages = _worker_job_for(ctx, task, node, siblings, _figure_payload(fig) if fig is not None else None,
                                         existing)
        nodes[node.id] = {"task": task, "node": node, "cards": [], "messages": _messages}
        jobs.append(job)

    def on_start(job):
        tracer.start(nodes[job.id]["node"])

    def on_done(res):
        entry = nodes[res.job.id]
        node, task = entry["node"], entry["task"]
        if not res.ok:
            logger.warning("Worker task %s failed: %s", node.id, res.error)
            entry["error"] = res.error
            tracer.log_call(node, "worker", None, messages=entry["messages"], error=res.error,
                            prompt_version=workers_mod.WORKER_PROMPT_VERSION)
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        value = res.value or {}
        rows, auto_deleted = _persist_cards(ctx, node, task, value.get("cards") or [], origin_tag=origin_tag)
        entry["cards"] = rows
        result = {"truncated": value.get("truncated"), "attempts": value.get("attempts"), "auto_deleted": auto_deleted}
        # Log the call under the node (cache hits are logged too, flagged cached, zero cost).
        cr = ChatResult(content=value.get("content") or "", message={}, usage=res.usage or {},
                        model=value.get("model") or node.model)
        tracer.log_call(node, "worker", cr, messages=entry["messages"], prompt_version=workers_mod.WORKER_PROMPT_VERSION,
                        parsed={"cards": value.get("cards")}, source_id=ctx.source_ids.get(task.unit_idxs[0]) if task.unit_idxs else None,
                        cached=res.cached)
        tracer.finish(node, status="cached" if res.cached else "done", cards_made=len(rows),
                      cards_kept=sum(1 for r in rows if r.status == "ok"), result=result)

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    return nodes


def _phase_write(ctx):
    tracer = ctx.tracer
    text_tasks, figure_tasks = ctx.plan.text_tasks, ctx.plan.figure_tasks
    tracer.phase("write")
    if not text_tasks and not figure_tasks:
        tracer.end_phase("write", status="failed", error="The plan contains no tasks.")
        raise OpenRouterError("The planner produced no work; nothing to write.")
    written = _run_write_tasks(ctx, "write", text_tasks) if text_tasks else {}
    if figure_tasks:
        # After the text, so each figure's writer is shown the cards its unit already has
        # and writes only what the figure adds to them.
        written.update(_run_write_tasks(ctx, "write", figure_tasks, after_existing=True))
    made = sum(len(e["cards"]) for e in written.values())
    failed = sum(1 for e in written.values() if e["node"].status == "failed")
    tracer.end_phase("write", result={"tasks": len(written), "cards": made, "failed_tasks": failed})
    if made == 0:
        # Offline, every task fails the same way. Say so rather than blame the source.
        for entry in written.values():
            if isinstance(entry.get("error"), OpenRouterConnectionError):
                raise entry["error"]
        raise OpenRouterError("No cards could be generated from this source.")
    return written


# ------------------------------------------------------------------- critique
def _run_critic(ctx, phase, entries):
    """entries: iterable of {"task", "node", "cards"}; critiques the ok cards of each in batches."""
    tracer = ctx.tracer
    cfg = current_app.config
    jobs = []
    batches = {}
    for entry in entries:
        cards = [c for c in entry["cards"] if c.status == "ok"]
        if not cards:
            continue
        source_text = ctx.source_text_for(entry["task"].unit_idxs)
        fig = db.session.get(Figure, entry["task"].figure_id) if entry["task"].figure_id else None
        if fig is not None:
            source_text = critic_mod.figure_source(source_text, figures_mod.describe_figure(_figure_payload(fig)))
        for start in range(0, len(cards), critic_mod.BATCH_SIZE):
            batch = cards[start : start + critic_mod.BATCH_SIZE]
            dicts = [_card_dict(c) for c in batch]
            node = tracer.task(
                phase, f"Critique {len(batch)} cards · {entry['node'].label[:80]}", strategy=entry["task"].strategy,
                unit_ids=list(entry["task"].unit_idxs), model=ctx.client.model_for("critic"), target_cards=len(batch),
                parent=tracer.phase_nodes.get(phase),
            )
            key = make_key("critic", ctx.client.model_for("critic"), critic_mod.CRITIC_PROMPT_VERSION, dicts, hash_text(source_text),
                           *ctx.client.effort_key("cold_reader", "judge"))
            batches[node.id] = {"node": node, "cards": batch}
            jobs.append(Job(
                id=node.id,
                fn=(lambda d=dicts, s=source_text: critic_mod.run_critic_batch(ctx.client, d, s)),
                cache_key=key, meta={"role": "critic", "model": ctx.client.model_for("critic")},
            ))

    def on_start(job):
        tracer.start(batches[job.id]["node"])

    def on_done(res):
        entry = batches[res.job.id]
        node = entry["node"]
        if not res.ok:
            logger.warning("Critic batch %s failed: %s", node.id, res.error)
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        verdicts = (res.value or {}).get("verdicts") or {}
        kept = dropped = rewritten = 0
        for i, card in enumerate(entry["cards"]):
            verdict = verdicts.get(i) or verdicts.get(str(i))
            if not verdict:
                continue
            new_card, status, tags = critic_mod.apply_verdict(_card_dict(card), verdict)
            card.type = new_card["type"]
            card.front = new_card.get("front")
            card.back = new_card.get("back")
            card.cloze_text = new_card.get("cloze_text")
            card.extra = new_card.get("extra")
            card.difficulty = new_card.get("difficulty")
            card.status = status
            card.tags = _dedupe_tags(list(card.tags or []) + tags + ([f"difficulty:{card.difficulty}"] if card.difficulty else []))
            card.critic_json = {k: verdict.get(k) for k in (
                "verdict", "reason", "supported", "atomic", "ambiguous", "leaks_answer", "cold_answer_correct", "cold_answer",
                "worthwhile", "difficulty",
            )}
            if status == "deleted":
                dropped += 1
            elif "critic:rewritten" in tags:
                rewritten += 1
                kept += 1
            else:
                kept += 1
        db.session.commit()
        tracer.finish(node, status="cached" if res.cached else "done", cards_made=len(entry["cards"]), cards_kept=kept,
                      usage=res.usage, cached=res.cached, result={"dropped": dropped, "rewritten": rewritten})

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    return len(jobs)


def _phase_critique(ctx, written):
    tracer = ctx.tracer
    cfg = current_app.config
    if not cfg.get("PIPELINE_CRITIC_ENABLED", True):
        tracer.phase("critique", status="skipped")
        tracer.end_phase("critique", status="skipped")
        return
    tracer.phase("critique")
    n = _run_critic(ctx, "critique", written.values())
    ok = Card.query.filter_by(deck_id=ctx.deck.id, status="ok").count()
    tracer.end_phase("critique", result={"batches": n, "cards_ok": ok})


def _card_dict(card):
    return {
        "type": card.type, "front": card.front, "back": card.back, "cloze_text": card.cloze_text, "extra": card.extra,
        "tags": list(card.tags or []), "source_quote": card.source_quote, "strategy": card.strategy,
    }


# ------------------------------------------------------------------- coverage
def _phase_coverage(ctx):
    """Audit each unit for testable facts no card covers, write cards for the gaps, and
    let through only the ones that earn a place (`_review_backfill`)."""
    tracer = ctx.tracer
    cfg = current_app.config
    if not cfg.get("PIPELINE_COVERAGE_ENABLED", True):
        tracer.phase("coverage", status="skipped")
        tracer.end_phase("coverage", status="skipped")
        return
    tracer.phase("coverage")
    # Only audit units a text task of the plan covers: a unit the user (or planner) left
    # without one was skipped on purpose and must not be back-filled here.
    covered = {i for t in ctx.plan.text_tasks for i in t.unit_idxs}
    live_units = [u for u in ctx.units if not u.skipped and u.idx in covered and (u.density or 3) >= 2]
    cards_by_unit = _cards_by_unit(ctx)
    model = ctx.client.model_for("reconcile")
    nodes = {}
    jobs = []
    for u in live_units:
        lines = _card_lines(cards_by_unit.get(u.idx, []))
        node = tracer.task("coverage", f"Audit coverage · {u.title[:90]}", unit_ids=[u.idx], model=model)
        key = make_key("coverage", model, reconcile_mod.COVERAGE_PROMPT_VERSION, hash_text(u.text), lines,
                       *ctx.client.effort_key("coverage"))
        nodes[node.id] = {"node": node, "unit": u}
        jobs.append(Job(id=node.id, fn=(lambda unit=u, c=lines: reconcile_mod.audit_unit(ctx.client, unit, c)),
                        cache_key=key, meta={"role": "reconcile", "model": model}))

    # A deck the student sized themselves is back-filled only for what an examiner would
    # very likely ask; one left on auto also for the plausible questions.
    min_importance = 3 if planner_mod.requested_cards(ctx.settings) else 2
    gap_tasks = []
    scores = {}

    def on_start(job):
        tracer.start(nodes[job.id]["node"])

    def on_done(res):
        entry = nodes[res.job.id]
        node, unit = entry["node"], entry["unit"]
        if not res.ok:
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        data = res.value or {}
        missing = [m for m in data.get("missing") or [] if m.get("importance", 1) >= min_importance]
        scores[unit.idx] = data.get("score")
        tracer.finish(node, status="cached" if res.cached else "done", usage=res.usage, cached=res.cached,
                      result={"score": data.get("score"), "missing": len(missing), "facts": [m["fact"] for m in missing][:12]})
        if missing:
            facts = "\n".join(f"- {m['fact']}" + (f" (source: \"{m['source_quote']}\")" if m.get("source_quote") else "") for m in missing[:14])
            gap_tasks.append(planner_mod.PlanTask(
                id=0, unit_idxs=[unit.idx], strategy=DEFAULT_STRATEGY, target_cards=min(12, len(missing)),
                notes="Coverage gap-fill. Write cards ONLY for these facts the first pass missed, one card per fact, "
                      "and leave out any fact a card the deck already has covers:\n" + facts,
                origin="coverage",
            ))

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)

    candidates = rejected = filled = 0
    if gap_tasks:
        kept_total = Card.query.filter_by(deck_id=ctx.deck.id, status="ok").count()
        gap_cap = max(6, int(kept_total * BACKFILL_SHARE))
        total_target = sum(t.target_cards for t in gap_tasks)
        if total_target > gap_cap:
            scale = gap_cap / float(total_target)
            for t in gap_tasks:
                t.target_cards = max(1, int(round(t.target_cards * scale)))
        written = _run_write_tasks(ctx, "coverage", gap_tasks, origin_tag="origin:coverage", after_existing=True)
        if cfg.get("PIPELINE_CRITIC_ENABLED", True):
            _run_critic(ctx, "coverage", written.values())
        candidates = sum(1 for e in written.values() for c in e["cards"] if c.status == "ok")
        rejected = _review_backfill(ctx, written)
        filled = sum(1 for e in written.values() for c in e["cards"] if c.status == "ok")
    tracer.end_phase("coverage", result={"audited": len(jobs), "gap_tasks": len(gap_tasks), "candidates": candidates,
                                          "rejected": rejected, "cards_added": filled, "scores": scores})


def _review_backfill(ctx, written):
    """The gate a back-fill card passes before it joins the deck. The audit only says a
    fact looked uncovered and the critic only checks a card against the source; neither
    sees the candidates next to the cards the unit already has. This review does, and it
    turns away repeats and cards not worth asking. A turned-away card is kept as
    `deleted` with the reason, so it can be read and restored. Returns how many were."""
    tracer = ctx.tracer
    model = ctx.client.model_for("critic")
    cards_by_unit = _cards_by_unit(ctx)
    nodes = {}
    jobs = []
    for entry in written.values():
        candidates = [c for c in entry["cards"] if c.status == "ok"]
        unit = ctx.unit_by_idx.get(entry["task"].unit_idxs[0]) if entry["task"].unit_idxs else None
        if not candidates or unit is None:
            continue
        new_ids = {c.id for c in candidates}
        existing = _card_lines(c for c in cards_by_unit.get(unit.idx, []) if c.id not in new_ids)
        dicts = [_card_dict(c) for c in candidates]
        node = tracer.task(
            "coverage", f"Review {len(candidates)} back-fill card{'s' if len(candidates) != 1 else ''} · {unit.title[:80]}",
            unit_ids=[unit.idx], model=model, target_cards=len(candidates),
        )
        key = make_key("backfill_review", model, reconcile_mod.ADDITIONS_PROMPT_VERSION, hash_text(unit.text), existing, dicts,
                       *ctx.client.effort_key("gatekeeper"))
        nodes[node.id] = {"node": node, "cards": candidates}
        jobs.append(Job(
            id=node.id,
            fn=(lambda u=unit, e=existing, d=dicts: reconcile_mod.review_additions(ctx.client, u, e, d)),
            cache_key=key, meta={"role": "critic", "model": model},
        ))

    rejected = [0]

    def on_start(job):
        tracer.start(nodes[job.id]["node"])

    def on_done(res):
        entry = nodes[res.job.id]
        node, cards = entry["node"], entry["cards"]
        if not res.ok:
            # Unreviewed is not approved: the cards wait for the student instead of joining
            # the deck on the audit's word alone.
            logger.warning("Back-fill review %s failed: %s", node.id, res.error)
            for card in cards:
                card.status = "needs_review"
                card.tags = _dedupe_tags(list(card.tags or []) + ["backfill:unreviewed"])
            db.session.commit()
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        decisions = (res.value or {}).get("decisions") or {}
        reasons = []
        for i, card in enumerate(cards):
            decision = decisions.get(i) or decisions.get(str(i)) or {}
            if decision.get("add"):
                continue
            reason = decision.get("reason") or "The reviewer did not approve it."
            card.status = "deleted"
            card.tags = _dedupe_tags(list(card.tags or []) + ["backfill:rejected"])
            card.critic_json = {**(card.critic_json or {}), "verdict": "drop", "reason": f"Back-fill review: {reason}"}
            reasons.append(reason)
        rejected[0] += len(reasons)
        db.session.commit()
        tracer.finish(node, status="cached" if res.cached else "done", cards_made=len(cards),
                      cards_kept=len(cards) - len(reasons), usage=res.usage, cached=res.cached,
                      result={"rejected": len(reasons), "reasons": reasons[:12]})

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    return rejected[0]


# ------------------------------------------------------------------ reconcile
def _phase_reconcile(ctx):
    tracer = ctx.tracer
    cfg = current_app.config
    tracer.phase("reconcile")
    cards = Card.query.filter_by(deck_id=ctx.deck.id, status="ok").order_by(Card.id).all()
    dicts = [_card_dict(c) for c in cards]
    exact = reconcile_mod.exact_duplicate_indices(dicts)
    for i in exact:
        cards[i].status = "deleted"
        cards[i].tags = _dedupe_tags(list(cards[i].tags or []) + ["dedupe:exact"])
    db.session.commit()
    live = [(i, c) for i, c in enumerate(cards) if c.status == "ok"]
    node = tracer.task("reconcile", f"Cluster {len(live)} cards by meaning", model=ctx.client.embedding_model)
    tracer.start(node)
    dropped = 0
    decisions = []
    if cfg.get("PIPELINE_EMBED_DEDUPE_ENABLED", True) and len(live) >= 2:
        try:
            texts = [reconcile_mod.card_text_for_embedding(dicts[i]) for i, _ in live]
            vectors = []
            for start in range(0, len(texts), 96):
                vectors.extend(ctx.client.embed(texts[start : start + 96]))
            clusters = reconcile_mod.cluster_by_similarity(vectors, float(cfg.get("PIPELINE_DEDUPE_THRESHOLD", 0.9)))
            tracer.finish(node, result={"clusters": len(clusters), "embedded": len(texts)})
            if clusters:
                # Map local positions back to card indices.
                pos_to_idx = [i for i, _ in live]
                clusters_idx = [[pos_to_idx[p] for p in cl] for cl in clusters]
                merge_node = tracer.task("reconcile", f"Resolve {len(clusters)} duplicate clusters",
                                         model=ctx.client.model_for("reconcile"))
                tracer.start(merge_node)
                try:
                    resolved = reconcile_mod.resolve_clusters(ctx.client, clusters_idx, dicts)
                except Exception as exc:
                    if is_terminal_error(exc):
                        raise
                    logger.warning("Cluster resolution failed (%s); keeping first of each cluster", exc)
                    resolved = {"drop": {i for cl in clusters_idx for i in cl[1:]}, "usage": {}, "decisions": []}
                for i in resolved["drop"]:
                    cards[i].status = "deleted"
                    cards[i].tags = _dedupe_tags(list(cards[i].tags or []) + ["dedupe:near_duplicate"])
                    dropped += 1
                decisions = resolved.get("decisions") or []
                db.session.commit()
                tracer.finish(merge_node, cards_made=sum(len(c) for c in clusters_idx), cards_kept=sum(len(c) for c in clusters_idx) - dropped,
                              usage=resolved.get("usage"), result={"dropped": dropped, "decisions": decisions[:30]})
        except Exception as exc:
            if is_terminal_error(exc):
                raise
            logger.warning("Embedding dedupe skipped: %s", exc)
            tracer.finish(node, status="failed", error=f"Embedding dedupe skipped: {exc}")
    else:
        tracer.finish(node, status="skipped", result={"reason": "disabled or too few cards"})
    tracer.end_phase("reconcile", result={"exact_duplicates": len(exact), "near_duplicates": dropped})


# --------------------------------------------------------------------- finish
def _topological_unit_order(units):
    order = []
    seen = set()
    by_idx = {u.idx: u for u in units}

    def visit(idx, stack):
        if idx in seen or idx in stack:
            return
        stack.add(idx)
        for dep in by_idx[idx].depends_on or []:
            if dep in by_idx:
                visit(dep, stack)
        stack.discard(idx)
        seen.add(idx)
        order.append(idx)

    for u in sorted(units, key=lambda u: u.idx):
        visit(u.idx, set())
    return {idx: rank for rank, idx in enumerate(order)}


def _phase_finish(ctx):
    tracer = ctx.tracer
    deck = ctx.deck
    tracer.phase("finish")
    ranks = _topological_unit_order(ctx.units)
    sid_to_idx = {sid: idx for idx, sid in ctx.source_ids.items()}
    cards = Card.query.filter_by(deck_id=deck.id).order_by(Card.id).all()
    counter = defaultdict(int)
    for c in cards:
        idx = sid_to_idx.get(c.source_id, 0)
        rank = ranks.get(idx, idx)
        counter[rank] += 1
        c.order_key = rank * 1000 + counter[rank]
    db.session.commit()
    ok = sum(1 for c in cards if c.status == "ok")
    if ok == 0:
        raise OpenRouterError("No valid cards survived validation and review.")
    review = sum(1 for c in cards if c.status == "needs_review")
    deleted = sum(1 for c in cards if c.status == "deleted")
    by_strategy = defaultdict(int)
    # Where the kept cards came from, next to the estimate the planner started from: the
    # estimate is not a cap, so the run says how far it went past it and why.
    by_origin = {"text": 0, "figures": 0, "backfill": 0}
    for c in cards:
        if c.status == "ok":
            by_strategy[c.strategy or "general"] += 1
            by_origin["backfill" if "origin:coverage" in (c.tags or []) else "figures" if c.figure_id else "text"] += 1
    stats = {
        "cards_ok": ok, "cards_needs_review": review, "cards_deleted": deleted, "by_strategy": dict(by_strategy),
        "budget": ctx.plan.budget, "by_origin": by_origin,
        "backfill_rejected": sum(1 for c in cards if "backfill:rejected" in (c.tags or [])),
        "units": len(ctx.units), "units_skipped": sum(1 for u in ctx.units if u.skipped),
        "cache_hits": ctx.cache.hits, "cache_misses": ctx.cache.misses,
    }
    tracer.set_run(phase="done", stats=stats, flagged=deleted + review)
    tracer.end_phase("finish", result=stats)
    deck.status = "ready"
    db.session.commit()
    logger.info("Deck %s ready: %s ok, %s review, %s deleted, cost $%.4f", deck.id, ok, review, deleted, tracer.totals["cost"])


# ------------------------------------------------------------ progress helper
def progress_for(deck):
    """Weighted phase progress in [0, 100] plus per-phase state, from PipelineTask rows."""
    tasks = PipelineTask.query.filter_by(deck_id=deck.id).order_by(PipelineTask.seq).all()
    phases = {}
    for t in tasks:
        if t.kind == "phase":
            phases[t.phase] = {"status": t.status, "done": 0, "total": 0, "node": t}
    for t in tasks:
        if t.kind != "phase" and t.phase in phases:
            phases[t.phase]["total"] += 1
            if t.status in ("done", "cached", "failed", "skipped"):
                phases[t.phase]["done"] += 1
    weights = {key: PHASE_WEIGHTS[key] for key, _label in phases_for(deck.settings_json)}
    total_weight = sum(weights.values())
    score = 0.0
    for phase, weight in weights.items():
        p = phases.get(phase)
        if not p:
            continue
        if p["status"] in ("done", "skipped", "failed"):
            score += weight
        elif p["status"] == "running":
            frac = (p["done"] / p["total"]) if p["total"] else 0.15
            score += weight * min(0.97, frac)
    if deck.status == "ready":
        score = total_weight
    return int(round(100 * score / total_weight)), phases, tasks
