"""The generation run: map -> plan -> (figures) -> write -> critique -> reconcile ->
coverage -> finish.

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

from ...extensions import db
from ...models import Card, Deck, Figure, LLMRun, Source
from ..chunking import clean_text, hash_text
from ..llm import OpenRouterError, TERMINAL_ERROR_MARKERS, is_terminal_error
from ..validators import is_math_valid, is_valid_cloze
from . import critic as critic_mod
from . import document_map as map_mod
from . import figures as figures_mod
from . import planner as planner_mod
from . import reconcile as reconcile_mod
from . import workers as workers_mod
from .cache import DBCache, NullCache, make_key
from .parallel import Job, run_jobs
from .routing import LLMClient
from .strategies import DEFAULT_STRATEGY, PROMPT_VERSION
from .trace import Tracer

logger = logging.getLogger(__name__)

PHASE_WEIGHTS = {
    "map": 5, "plan": 10, "figures": 5, "write": 42, "critique": 20, "reconcile": 5, "coverage": 10, "finish": 3,
}


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
    if isinstance(exc, OpenRouterError):
        status = exc.status_code
        detail = (exc.response_body or "").lower()
        if status == 429:
            if any(marker in detail for marker in TERMINAL_ERROR_MARKERS):
                return "OpenRouter credits/quota were exhausted while processing this deck."
            return "OpenRouter rate limit was hit while processing this deck. Wait a minute and try again."
        if status in (401, 403):
            return "OpenRouter authentication failed. Check your API key and model access."
        if status == 400:
            return f"OpenRouter rejected a request: {exc.response_body or exc}"
        if status and status >= 500:
            return "OpenRouter is temporarily unavailable. Please try again shortly."
        return str(exc)
    if isinstance(exc, RuntimeError) and "OPENROUTER_API_KEY" in str(exc):
        return "OpenRouter API key is not configured. Set OPENROUTER_API_KEY and retry."
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
    figure_tasks: list = field(default_factory=list)

    def units_for(self, idxs):
        return [self.unit_by_idx[i] for i in idxs if i in self.unit_by_idx]

    def source_text_for(self, idxs):
        return "\n\n".join(u.text for u in self.units_for(idxs))


def _build_context(deck):
    cfg = current_app.config
    settings = dict(deck.settings_json or {})
    settings.setdefault("card_style", deck.card_style)
    client = LLMClient(cfg)
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

    if resume_from_plan and (deck.run_json or {}).get("plan"):
        _restore_plan(ctx)
    else:
        tracer.clear()
        tracer.set_run(
            phase="map", last_error=None, plan=None, summary=None, started=True,
            prompt_version=PROMPT_VERSION, model=ctx.client.default_model,
        )
        _phase_map(ctx)
        _phase_plan(ctx)
        if ctx.settings.get("review_plan"):
            deck.status = "planned"
            tracer.set_run(phase="planned")
            db.session.commit()
            logger.info("Deck %s planned; waiting for review", deck.id)
            return deck.id

    _phase_figures(ctx)
    written = _phase_write(ctx)
    _phase_critique(ctx, written)
    _phase_reconcile(ctx)
    _phase_coverage(ctx)
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


# ----------------------------------------------------------------------- plan
def _phase_plan(ctx):
    tracer = ctx.tracer
    deck = ctx.deck
    cfg = current_app.config
    tracer.phase("plan")
    node = tracer.task("plan", "Plan the work order", model=ctx.client.model_for("planner"))
    tracer.start(node)
    requested = ctx.settings.get("target_cards")
    try:
        requested = int(requested) if requested not in (None, "", "auto", 0, "0") else None
    except (TypeError, ValueError):
        requested = None
    budget = planner_mod.suggest_budget(ctx.units, requested)
    figure_counts = _figure_counts_by_unit(ctx)
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
            figure_counts=figure_counts, on_event=on_event,
        )
    except Exception as exc:
        if is_terminal_error(exc):
            tracer.finish(node, status="failed", error=exc)
            raise
        logger.warning("Planner failed (%s); using heuristic plan", exc)
        tracer.log_call(node, "planner", None, error=exc, prompt_version=planner_mod.PLAN_PROMPT_VERSION)
        plan = planner_mod.plan_heuristic(ctx.units, budget)
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

    # ---- Map + plan succeeded: now it is safe to replace the previous generation.
    _wipe_previous_generation(deck)
    _persist_units(ctx)
    tracer.finish(
        node,
        result={
            "events": events[-40:], "tasks": len(plan.tasks), "skips": len(plan.skips), "budget": budget,
            "turns": plan.turns, "mode": plan.mode, "summary": plan.summary,
        },
    )
    tracer.set_run(plan=plan.to_dict(), doc_meta=ctx.doc_meta, budget=budget, summary=plan.summary)
    tracer.end_phase("plan", result={"tasks": len(plan.tasks), "summary": plan.summary, "mode": plan.mode})


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
    from ...models import PipelineTask

    last = PipelineTask.query.filter_by(deck_id=deck.id).order_by(PipelineTask.seq.desc()).first()
    ctx.tracer._seq = last.seq if last else 0
    for node in PipelineTask.query.filter_by(deck_id=deck.id, kind="phase").all():
        ctx.tracer.phase_nodes[node.phase] = node
    # Cards from a previous completed run of this plan (re-run) must not linger.
    Card.query.filter_by(deck_id=deck.id).delete()
    db.session.commit()
    ctx.tracer.set_run(phase="figures", last_error=None)


def _unit_idx_for_page(ctx, page):
    if page is None:
        return None
    best = None
    for u in ctx.units:
        if u.page_start and u.page_end and u.page_start <= page <= u.page_end:
            return u.idx
        if u.page_start and (best is None or abs(u.page_start - page) < abs(ctx.unit_by_idx[best].page_start - page)):
            best = u.idx
    return best


def _figure_counts_by_unit(ctx):
    counts = defaultdict(int)
    for fig in Figure.query.filter_by(deck_id=ctx.deck.id).all():
        idx = _unit_idx_for_page(ctx, fig.page)
        if idx is not None:
            counts[idx] += 1
    return dict(counts)


# -------------------------------------------------------------------- figures
def _phase_figures(ctx):
    tracer = ctx.tracer
    cfg = current_app.config
    figs = Figure.query.filter_by(deck_id=ctx.deck.id).order_by(Figure.page).all()
    if not figs or not cfg.get("PIPELINE_FIGURES_ENABLED", True) or not ctx.settings.get("use_figures", True):
        tracer.phase("figures", status="skipped")
        tracer.end_phase("figures", status="skipped")
        return
    tracer.phase("figures")
    nodes = {}
    jobs = []
    for fig in figs:
        idx = _unit_idx_for_page(ctx, fig.page)
        unit = ctx.unit_by_idx.get(idx) if idx is not None else None
        context = unit.text if unit else ""
        node = tracer.task("figures", f"Read figure on p.{fig.page}", strategy="vision",
                           unit_ids=[idx] if idx is not None else [], model=ctx.client.model_for("vision"))
        nodes[fig.id] = node
        key = make_key("vision", ctx.client.model_for("vision"), figures_mod.VISION_PROMPT_VERSION, fig.hash, context[:6000])
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
        fig.analysis_json = {k: data.get(k) for k in ("parts", "facts", "suggested_cards")}
        db.session.commit()
        if fig.useful and int(data.get("suggested_cards") or 0) > 0:
            idx = _unit_idx_for_page(ctx, fig.page)
            if idx is not None:
                ctx.figure_tasks.append(
                    planner_mod.PlanTask(
                        id=0, unit_idxs=[idx], strategy="figure_recall",
                        target_cards=max(1, min(8, int(data.get("suggested_cards") or 2))),
                        notes=f"Figure on p.{fig.page}: {fig.caption}", origin="figure", figure_id=fig.id,
                    )
                )
        tracer.finish(
            node, status="cached" if res.cached else "done", usage=res.usage, cached=res.cached,
            result={"useful": fig.useful, "kind": fig.kind, "caption": fig.caption},
        )

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)
    failed = sum(1 for res in results.values() if not res.ok)
    useful = sum(1 for res in results.values() if res.ok and (res.value or {}).get("useful"))
    error = f"{failed} of {len(figs)} figures could not be analyzed. See the failed figure tasks for details." if failed else None
    tracer.end_phase("figures", status="failed" if failed else "done", error=error,
                     result={"figures": len(figs), "useful": useful, "tasks": len(ctx.figure_tasks), "failed": failed})
    logger.info("Deck %s figures: %s found, %s useful, %s tasks, %s failed",
                ctx.deck.id, len(figs), useful, len(ctx.figure_tasks), failed)


# ---------------------------------------------------------------------- write
def _persist_cards(ctx, node, task, raw_cards, origin_tag=None):
    """Validate + store worker output. Returns (rows, auto_deleted)."""
    from ..schemas import CardSchema

    rows = []
    auto_deleted = 0
    unit_idx = task.unit_idxs[0] if task.unit_idxs else None
    source_id = ctx.source_ids.get(unit_idx)
    for raw in raw_cards:
        try:
            model = CardSchema.model_validate(raw)
        except Exception:
            continue
        card = workers_mod.normalize_card(model, strategy=task.strategy)
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
                deck_id=ctx.deck.id, source_id=source_id, task_id=node.id, figure_id=task.figure_id,
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


def _worker_job_for(ctx, task, node, siblings, figure_payload=None):
    units = ctx.units_for(task.unit_idxs)
    messages = workers_mod.build_worker_messages(task, units, ctx.settings, ctx.card_style, siblings=siblings,
                                                figure=figure_payload)
    model = ctx.client.model_for("worker")
    key = make_key("worker", model, workers_mod.WORKER_PROMPT_VERSION, messages)
    return Job(
        id=node.id,
        fn=(lambda m=messages, t=task: {**workers_mod.run_worker(ctx.client, m, t.target_cards), "messages": m}),
        cache_key=key, meta={"role": "worker", "model": model, "task": task},
    ), messages


def _run_write_tasks(ctx, phase, tasks, origin_tag=None):
    """Shared by the main write phase and the coverage gap-fill round.
    Returns {node_id: {"task": PlanTask, "node": node, "cards": [Card rows]}}."""
    tracer = ctx.tracer
    nodes = {}
    jobs = []
    by_unit = defaultdict(list)
    for t in tasks:
        for i in t.unit_idxs:
            by_unit[i].append(t)
    for task in tasks:
        units = ctx.units_for(task.unit_idxs)
        title = ", ".join(u.title for u in units)[:150] or f"units {task.unit_idxs}"
        node = tracer.task(
            phase, f"{title}", strategy=task.strategy, unit_ids=list(task.unit_idxs),
            model=ctx.client.model_for("worker"), target_cards=task.target_cards, notes=task.notes,
        )
        siblings = []
        for i in task.unit_idxs:
            for other in by_unit[i]:
                if other is not task and (other.strategy, tuple(other.unit_idxs), other.notes) not in [(s[0], tuple(s[1]), s[2]) for s in siblings]:
                    siblings.append((other.strategy, other.unit_idxs, other.notes))
        figure_payload = None
        if task.figure_id:
            fig = db.session.get(Figure, task.figure_id)
            if fig is not None:
                analysis = fig.analysis_json or {}
                figure_payload = {
                    "caption": fig.caption, "description": fig.description,
                    "parts": "; ".join(analysis.get("parts") or []), "facts": "; ".join(analysis.get("facts") or []),
                }
        job, _messages = _worker_job_for(ctx, task, node, siblings, figure_payload)
        nodes[node.id] = {"task": task, "node": node, "cards": [], "messages": _messages}
        jobs.append(job)

    def on_start(job):
        tracer.start(nodes[job.id]["node"])

    def on_done(res):
        entry = nodes[res.job.id]
        node, task = entry["node"], entry["task"]
        if not res.ok:
            logger.warning("Worker task %s failed: %s", node.id, res.error)
            tracer.log_call(node, "worker", None, messages=entry["messages"], error=res.error,
                            prompt_version=workers_mod.WORKER_PROMPT_VERSION)
            tracer.finish(node, status="failed", error=format_generation_error(res.error))
            return
        value = res.value or {}
        rows, auto_deleted = _persist_cards(ctx, node, task, value.get("cards") or [], origin_tag=origin_tag)
        entry["cards"] = rows
        result = {"truncated": value.get("truncated"), "attempts": value.get("attempts"), "auto_deleted": auto_deleted}
        # Log the call under the node (cache hits are logged too, flagged cached, zero cost).
        from .routing import ChatResult

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
    tasks = list(ctx.plan.tasks) + list(ctx.figure_tasks)
    tracer.phase("write")
    if not tasks:
        tracer.end_phase("write", status="failed", error="The plan contains no tasks.")
        raise OpenRouterError("The planner produced no work; nothing to write.")
    written = _run_write_tasks(ctx, "write", tasks)
    made = sum(len(e["cards"]) for e in written.values())
    failed = sum(1 for e in written.values() if e["node"].status == "failed")
    tracer.end_phase("write", result={"tasks": len(tasks), "cards": made, "failed_tasks": failed})
    if made == 0:
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
        for start in range(0, len(cards), critic_mod.BATCH_SIZE):
            batch = cards[start : start + critic_mod.BATCH_SIZE]
            dicts = [_card_dict(c) for c in batch]
            node = tracer.task(
                phase, f"Critique {len(batch)} cards · {entry['node'].label[:80]}", strategy=entry["task"].strategy,
                unit_ids=list(entry["task"].unit_idxs), model=ctx.client.model_for("critic"), target_cards=len(batch),
                parent=tracer.phase_nodes.get(phase),
            )
            key = make_key("critic", ctx.client.model_for("critic"), critic_mod.CRITIC_PROMPT_VERSION, dicts, hash_text(source_text))
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
                "verdict", "reason", "supported", "atomic", "ambiguous", "leaks_answer", "cold_answer_correct", "cold_answer", "difficulty",
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


# ------------------------------------------------------------------- coverage
def _phase_coverage(ctx):
    tracer = ctx.tracer
    cfg = current_app.config
    if not cfg.get("PIPELINE_COVERAGE_ENABLED", True):
        tracer.phase("coverage", status="skipped")
        tracer.end_phase("coverage", status="skipped")
        return
    tracer.phase("coverage")
    # Only audit units the plan actually covers: a unit the user (or planner) left
    # without a task was skipped on purpose and must not be back-filled here.
    covered = {i for t in list(ctx.plan.tasks) + list(ctx.figure_tasks) for i in t.unit_idxs}
    live_units = [u for u in ctx.units if not u.skipped and u.idx in covered and (u.density or 3) >= 2]
    cards_by_unit = defaultdict(list)
    for c in Card.query.filter_by(deck_id=ctx.deck.id, status="ok").all():
        cards_by_unit[c.source_id].append(c)
    nodes = {}
    jobs = []
    for u in live_units:
        sid = ctx.source_ids.get(u.idx)
        prompts = [critic_mod.card_prompt(_card_dict(c)) for c in cards_by_unit.get(sid, [])]
        node = tracer.task("coverage", f"Audit coverage · {u.title[:90]}", unit_ids=[u.idx],
                           model=ctx.client.model_for("reconcile"))
        key = make_key("coverage", ctx.client.model_for("reconcile"), reconcile_mod.COVERAGE_PROMPT_VERSION, hash_text(u.text), prompts)
        nodes[node.id] = {"node": node, "unit": u}
        jobs.append(Job(id=node.id, fn=(lambda unit=u, p=prompts: reconcile_mod.audit_unit(ctx.client, unit, p)),
                        cache_key=key, meta={"role": "reconcile", "model": ctx.client.model_for("reconcile")}))

    gap_tasks = []
    kept_total = sum(len(v) for v in cards_by_unit.values())
    gap_cap = max(6, int(kept_total * 0.4))
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
        missing = [m for m in data.get("missing") or [] if m.get("importance", 1) >= 2]
        scores[unit.idx] = data.get("score")
        tracer.finish(node, status="cached" if res.cached else "done", usage=res.usage, cached=res.cached,
                      result={"score": data.get("score"), "missing": len(missing), "facts": [m["fact"] for m in missing][:12]})
        if missing:
            facts = "\n".join(f"- {m['fact']}" + (f" (source: \"{m['source_quote']}\")" if m.get("source_quote") else "") for m in missing[:14])
            gap_tasks.append(planner_mod.PlanTask(
                id=0, unit_idxs=[unit.idx], strategy=DEFAULT_STRATEGY, target_cards=min(12, len(missing)),
                notes="Coverage gap-fill. Write cards ONLY for these facts the first pass missed:\n" + facts,
                origin="coverage",
            ))

    results = run_jobs(jobs, max_workers=ctx.max_workers, cache=ctx.cache, on_start=on_start, on_done=on_done,
                       abort_on=_abort_on)
    _raise_if_terminal(results)

    filled = 0
    if gap_tasks:
        total_target = sum(t.target_cards for t in gap_tasks)
        if total_target > gap_cap:
            scale = gap_cap / float(total_target)
            for t in gap_tasks:
                t.target_cards = max(1, int(round(t.target_cards * scale)))
        written = _run_write_tasks(ctx, "coverage", gap_tasks, origin_tag="origin:coverage")
        if cfg.get("PIPELINE_CRITIC_ENABLED", True):
            _run_critic(ctx, "coverage", written.values())
        filled = sum(sum(1 for c in e["cards"] if c.status == "ok") for e in written.values())
    tracer.end_phase("coverage", result={"audited": len(jobs), "gap_tasks": len(gap_tasks), "cards_added": filled,
                                          "scores": scores})


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
    for c in cards:
        if c.status == "ok":
            by_strategy[c.strategy or "general"] += 1
    stats = {
        "cards_ok": ok, "cards_needs_review": review, "cards_deleted": deleted, "by_strategy": dict(by_strategy),
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
    from ...models import PipelineTask

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
    total_weight = sum(PHASE_WEIGHTS.values())
    score = 0.0
    for phase, weight in PHASE_WEIGHTS.items():
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
