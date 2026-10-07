"""Phase 1 — the planner.

The planner is a real agent loop: it sees the document map, can read any unit in full,
can search the source, and spawns worker tasks with a strategy, a card budget, and
notes. It decides how the material is carved up — the thing the old pipeline hard-coded
as `max_chars=3500`.

It decides the figures too. The vision pass has already read each one, so the planner
sees what a figure shows next to the unit it sits in and rules on it like on any other
material: a task of its own, or no cards. That keeps every card count in one plan, where
it is weighed against the text and shows up on the plan review.

The budget it is given is an estimate, not a cap: it is told to go over where the
material holds more than its size suggests. What is fixed are the safety rails: every
non-skipped unit must end up covered (uncovered units get a default task), every figure
ends up decided, task and card counts have a runaway ceiling, and if the model never
calls a tool we fall back to a single structured-output planning call.
"""

import logging
import re
from dataclasses import dataclass, field

from ..llm import extract_json, json_schema_format, run_tool_loop, tool_spec
from .cheatsheet import FIGURE_LINE_RE
from .strategies import DEFAULT_STRATEGY, FIGURE_STRATEGY, get_strategy, strategy_catalog, text_strategies

logger = logging.getLogger(__name__)

PLAN_PROMPT_VERSION = "plan-v2"

MAX_TASKS = 80
MAX_UNITS_PER_TASK = 4
MAX_TASK_CARDS = 60
MAX_FIGURE_CARDS = 12
READ_UNIT_CAP = 9000


@dataclass
class PlanTask:
    id: int
    unit_idxs: list
    strategy: str
    target_cards: int
    notes: str = ""
    origin: str = "planner"  # planner | auto | coverage | regenerate
    figure_id: int = None  # set on a figure task: the Figure its cards show

    def to_dict(self):
        return {
            "id": self.id,
            "unit_idxs": list(self.unit_idxs),
            "strategy": self.strategy,
            "target_cards": self.target_cards,
            "notes": self.notes,
            "origin": self.origin,
            "figure_id": self.figure_id,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=int(d.get("id") or 0),
            unit_idxs=[int(i) for i in d.get("unit_idxs") or []],
            strategy=d.get("strategy") or DEFAULT_STRATEGY,
            target_cards=int(d.get("target_cards") or 0),
            notes=d.get("notes") or "",
            origin=d.get("origin") or "planner",
            figure_id=d.get("figure_id"),
        )


@dataclass
class Plan:
    tasks: list = field(default_factory=list)
    skips: dict = field(default_factory=dict)  # unit idx -> reason
    figure_skips: dict = field(default_factory=dict)  # Figure.id -> reason it gets no cards
    summary: str = ""
    budget: int = 0
    turns: int = 0
    mode: str = "agent"  # agent | structured | heuristic

    def to_dict(self):
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "skips": {str(k): v for k, v in self.skips.items()},
            "figure_skips": {str(k): v for k, v in self.figure_skips.items()},
            "summary": self.summary,
            "budget": self.budget,
            "turns": self.turns,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d):
        plan = cls(
            tasks=[PlanTask.from_dict(t) for t in d.get("tasks") or []],
            skips={int(k): v for k, v in (d.get("skips") or {}).items()},
            figure_skips={int(k): v for k, v in (d.get("figure_skips") or {}).items()},
            summary=d.get("summary") or "",
            budget=int(d.get("budget") or 0),
            turns=int(d.get("turns") or 0),
            mode=d.get("mode") or "agent",
        )
        return plan

    @property
    def text_tasks(self):
        return [t for t in self.tasks if not t.figure_id]

    @property
    def figure_tasks(self):
        return [t for t in self.tasks if t.figure_id]


# ----------------------------------------------------------------------------- sizing
def requested_cards(settings):
    """The deck size the student typed, or None when it was left on auto."""
    value = (settings or {}).get("target_cards")
    try:
        value = int(value) if value not in (None, "", "auto") else 0
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else None


def _sizing_chars(unit):
    """A unit's length for sizing. A cheat-sheet line that only places a diagram is not
    text to write cards from: the figure is sized on its own."""
    return sum(len(line) + 1 for line in unit.text.split("\n") if not FIGURE_LINE_RE.match(line))


def heuristic_target(unit, strategy_key=DEFAULT_STRATEGY):
    """Cards a unit 'deserves' from its size and density; the planner's starting point."""
    strategy = get_strategy(strategy_key)
    density_factor = {1: 0.35, 2: 0.6, 3: 1.0, 4: 1.35, 5: 1.7}.get(int(unit.density or 3), 1.0)
    raw = (_sizing_chars(unit) / 1000.0) * strategy.cards_per_1k_chars * density_factor
    return max(2, min(45, int(round(raw))))


def suggest_budget(units, requested=None, figures=None):
    """The estimate the planner starts from: what the text is worth by size and density,
    plus what the vision pass thinks each figure adds. A number the student typed stands
    for the whole deck, figures included."""
    if requested and int(requested) > 0:
        return max(5, int(requested))
    live = [u for u in units if not u.skipped]
    live_idxs = {u.idx for u in live}
    auto = sum(heuristic_target(u) for u in live)
    auto += sum(int(f.get("suggested") or 0) for f in figures or [] if f.get("unit_idx") in live_idxs)
    return max(5, auto)


# ------------------------------------------------------------------------------ prompt
PLANNER_SYSTEM = """You are the planning agent of a flashcard generator. Your job is to decide HOW a study document is turned into Anki cards, then delegate the writing to worker agents by spawning tasks. You do not write cards yourself.

You have tools:
- read_unit(unit_idx): read a unit's full text. Use it whenever the summary is not enough to choose a strategy or a card budget.
- search_source(query): find where a term or topic appears across units.
- spawn_task(unit_idxs, strategy, target_cards, notes): delegate a card-writing task. One task = one worker call that reads the listed units verbatim. Group only adjacent, closely related units (max {max_units} per task). You may spawn several tasks with different strategies over the same unit when it deserves it (e.g. definition_sweep + pitfall_edge_case).
- skip_unit(unit_idx, reason): exclude a unit entirely (fluff, references, recap of earlier units, out of the student's focus).
- spawn_figure_task(figure, target_cards, notes): delegate cards on one figure. Its image is shown on every card the task writes. Only offered when the document has figures.
- skip_figure(figure, reason): give a figure no cards.
- finish_plan(summary): call this exactly once when every non-skipped unit is covered by at least one task.

Strategies (choose per task):
{catalog}

Planning principles
- Read first, then decide. Summaries can mislead; read any unit whose kind or density you are unsure about, and always read units marked figure_heavy or worked_example before assigning a strategy.
- Match the strategy to the unit's real structure. Mixed units get `general`; dense glossaries get `definition_sweep`; equation-heavy text gets `formula_derivation`; processes get `mechanism_chain`; confusable siblings get `compare_contrast`.
- Size each task to its content: `target_cards` is the number of high-value cards the material can honestly support. Dense units earn more; narrative earns fewer. The suggested overall budget is an estimate, not a limit: go above it where the material holds more examinable content than its size suggests, and below it where it holds less. Never pad.
- Notes are instructions to the worker. Make them specific and local: "one card per symbol in the rate-law table", "contrast the three isotherm models on their assumptions", "ignore the historical aside". Mention the student's focus/exclusions where relevant.
- Respect the student's focus and exclusions. Skip what they excluded; weight what they asked for.
- Do not spawn tasks for skipped units. Do not duplicate coverage across tasks unless the strategies differ.
- Figures, when the document has any, are listed under the document map with what a vision pass read off each one. Decide every figure. A figure earns cards only for what a student must recognise in the image or recall from it and that the text tasks of its unit will not already cover: usually one to three cards, often none. Skip a figure that repeats another figure, that only illustrates one worked example, or whose content the text states anyway. The vision pass's suggested count is advice, and a figure you leave undecided follows it.
- Be decisive: aim to finish in as few turns as possible while still reading what you need."""


def _unit_listing(units, figures=None):
    figure_counts = {}
    for f in figures or []:
        figure_counts[f.get("unit_idx")] = figure_counts.get(f.get("unit_idx"), 0) + 1
    lines = []
    for u in units:
        flags = []
        if u.skipped:
            flags.append(f"SKIPPED by mapper: {u.skip_reason or 'n/a'}")
        if figure_counts.get(u.idx):
            flags.append(f"{figure_counts[u.idx]} figure(s)")
        pages = ""
        if u.page_start:
            pages = f" · p.{u.page_start}" + (f"-{u.page_end}" if u.page_end and u.page_end != u.page_start else "")
        deps = f" · builds on {u.depends_on}" if u.depends_on else ""
        lines.append(
            f"[{u.idx}] {u.title!r} · kind={u.kind} · density={u.density} · {u.chars} chars{pages}{deps}"
            + (f" · {'; '.join(flags)}" if flags else "")
            + f"\n    summary: {u.summary or '(none)'}"
            + f"\n    suggested cards: {heuristic_target(u)}"
        )
    return "\n".join(lines)


def _figure_listing(figures):
    lines = []
    for f in figures:
        where = " · ".join(filter(None, [f"unit {f['unit_idx']}", f"p.{f['page']}" if f.get("page") else "", f.get("kind") or ""]))
        lines.append(
            f"F{f['number']}: {where} · vision suggests {int(f.get('suggested') or 0)} card(s)"
            + f"\n    caption: {' '.join((f.get('caption') or '(none)').split())}"
            + f"\n    adds beyond the text: {' '.join((f.get('adds') or '(not stated)').split())}"
        )
    return "\n".join(lines)


def _planner_user_message(units, settings, budget, doc_meta, figures=None):
    focus = settings.get("focus") or ""
    exclude = settings.get("exclude") or ""
    glossary = settings.get("glossary") or ""
    context = settings.get("exam_context") or ""
    card_style = settings.get("card_style") or "auto"
    live = [u for u in units if not u.skipped]
    figures = figures or []
    # A condensed unit reads like a recap; tell the planner it is the material itself.
    cheat_sheet_note = [
        "The unit texts are an exam cheat sheet already condensed from the student's material (kind and summary "
        "describe the original). Every line was kept because it is examinable: do not skip a unit for being terse "
        "or looking like a recap, and size each task to cover its whole unit. A line starting [[Figure N]] is a "
        "diagram kept on the sheet, not text to write cards from: its cards, if it earns any, come from its figure "
        "task, and a figure you give no cards is taken off the sheet."
    ] if settings.get("cheat_sheet") else []
    if requested_cards(settings):
        budget_line = (
            f"The student asked for about {budget} cards in total, figures included, across {len(live)} live units. "
            "That is the size of deck they want: stay close to it, and go over only for content that would otherwise "
            "be left out."
        )
    else:
        budget_line = (
            f"Suggested overall budget: about {budget} cards across {len(live)} live units"
            + (", figures included" if figures else "")
            + ". It is an estimate from the size and density of the material, not a limit."
        )
    figure_block = [
        "",
        f"Figures ({len(figures)}), each read from its page image by a vision pass:",
        _figure_listing(figures),
    ] if figures else []
    return "\n".join(
        [
            f"Subject: {doc_meta.get('subject') or 'unknown'}",
            f"Document summary: {doc_meta.get('document_summary') or '(none)'}",
            f"Student context / exam: {context or 'not given'}",
            f"Preferred card style: {card_style}",
            f"Focus: {focus or 'all exam-useful material'}",
            f"Exclude: {exclude or 'none'}",
            f"Must-include terms: {glossary or 'none'}",
            budget_line,
            *cheat_sheet_note,
            "",
            f"Document map ({len(units)} units):",
            _unit_listing(units, figures),
            *figure_block,
            "",
            "Plan the work. Read what you need, spawn tasks, skip what deserves skipping, "
            + ("decide every figure, " if figures else "")
            + "then call finish_plan.",
        ]
    )


def _tools(with_figures=False):
    tools = [
        tool_spec(
            "read_unit",
            "Read the full text of one unit of the document map.",
            {
                "type": "object",
                "properties": {"unit_idx": {"type": "integer", "description": "Unit index from the map."}},
                "required": ["unit_idx"],
                "additionalProperties": False,
            },
        ),
        tool_spec(
            "search_source",
            "Case-insensitive search across all units. Returns matching snippets with unit indexes.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        tool_spec(
            "spawn_task",
            "Delegate a card-writing task to a worker agent.",
            {
                "type": "object",
                "properties": {
                    "unit_idxs": {"type": "array", "items": {"type": "integer"}, "description": "Adjacent, related unit indexes (1-4)."},
                    "strategy": {"type": "string", "enum": list(text_strategies().keys())},
                    "target_cards": {"type": "integer", "description": "High-value cards this task should produce."},
                    "notes": {"type": "string", "description": "Specific instructions for the worker."},
                },
                "required": ["unit_idxs", "strategy", "target_cards", "notes"],
                "additionalProperties": False,
            },
        ),
        tool_spec(
            "skip_unit",
            "Exclude a unit from card generation.",
            {
                "type": "object",
                "properties": {"unit_idx": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["unit_idx", "reason"],
                "additionalProperties": False,
            },
        ),
    ]
    if with_figures:
        tools += [
            tool_spec(
                "spawn_figure_task",
                "Delegate cards on one figure. The figure's image is shown on every card the task writes.",
                {
                    "type": "object",
                    "properties": {
                        "figure": {"type": "integer", "description": "Figure number from the listing (F3 is 3)."},
                        "target_cards": {"type": "integer", "description": "Cards the figure earns beyond its unit's text cards."},
                        "notes": {"type": "string", "description": "What the worker should and should not ask about the figure."},
                    },
                    "required": ["figure", "target_cards", "notes"],
                    "additionalProperties": False,
                },
            ),
            tool_spec(
                "skip_figure",
                "Give a figure no cards.",
                {
                    "type": "object",
                    "properties": {"figure": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["figure", "reason"],
                    "additionalProperties": False,
                },
            ),
        ]
    tools.append(
        tool_spec(
            "finish_plan",
            "Finish planning. Call once every non-skipped unit is covered.",
            {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Two to four sentences explaining the plan to the student."}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        )
    )
    return tools


# ---------------------------------------------------------------------------- figures
def _figure_task(task_id, figure, target, notes="", origin="planner"):
    return PlanTask(
        id=task_id, unit_idxs=[figure["unit_idx"]], strategy=FIGURE_STRATEGY,
        target_cards=max(1, min(MAX_FIGURE_CARDS, int(target))), notes=(notes or "")[:1500], origin=origin,
        figure_id=figure["id"],
    )


def _decide_figure(plan, figure, target, notes="", origin="planner", reason=""):
    """Record one ruling on a figure, replacing any earlier one. `target` of 0 skips it."""
    plan.tasks[:] = [t for t in plan.tasks if t.figure_id != figure["id"]]
    plan.figure_skips.pop(figure["id"], None)
    if int(target or 0) <= 0:
        plan.figure_skips[figure["id"]] = (reason or "skipped by planner")[:400]
        return None
    task = _figure_task(max([t.id for t in plan.tasks] + [0]) + 1, figure, target, notes, origin)
    plan.tasks.append(task)
    return task


def settle_figures(units, plan, figures):
    """Every figure ends up with a task or a reason it has none. One the planner did not
    rule on follows the vision pass; one in a skipped unit gets no cards, whoever asked."""
    by_idx = {u.idx: u for u in units}
    known = {f["id"]: f for f in figures or []}

    def dead(idx):
        unit = by_idx.get(idx)
        return unit is None or unit.skipped or idx in plan.skips

    for task in plan.figure_tasks:
        if task.figure_id not in known or dead(task.unit_idxs[0] if task.unit_idxs else None):
            plan.tasks.remove(task)
            if task.figure_id in known:
                plan.figure_skips[task.figure_id] = "Its unit is skipped."
    decided = {t.figure_id for t in plan.figure_tasks} | set(plan.figure_skips)
    for figure in figures or []:
        if figure["id"] in decided:
            continue
        if dead(figure["unit_idx"]):
            plan.figure_skips[figure["id"]] = "Its unit is skipped."
        elif int(figure.get("suggested") or 0) > 0:
            _decide_figure(plan, figure, figure["suggested"], origin="auto",
                           notes="Auto-assigned: the planner did not rule on this figure, so it follows the vision pass.")
        else:
            plan.figure_skips[figure["id"]] = "The vision pass found nothing in it worth a card beyond the text."
    return plan


# --------------------------------------------------------------------------- planning
def plan_with_agent(client, units, settings, doc_meta, budget, max_turns=14, figures=None, on_event=None):
    """Run the planner loop. Returns (Plan, transcript, ChatResults list).

    `figures` are the figures up for a decision, each {id, number, unit_idx, page, kind,
    caption, adds, suggested}."""
    by_idx = {u.idx: u for u in units}
    figures = list(figures or [])
    figure_by_number = {f["number"]: f for f in figures}
    plan = Plan(budget=budget)
    calls = []

    def emit(kind, **payload):
        if on_event:
            try:
                on_event(kind, payload)
            except Exception:
                logger.exception("Planner event handler failed")

    def read_unit(args):
        idx = int(args.get("unit_idx"))
        unit = by_idx.get(idx)
        if unit is None:
            return {"error": f"No unit {idx}"}
        emit("read", unit_idx=idx)
        text = unit.text
        truncated = False
        if len(text) > READ_UNIT_CAP:
            text = text[:READ_UNIT_CAP]
            truncated = True
        return {
            "unit_idx": idx, "title": unit.title, "kind": unit.kind, "density": unit.density,
            "chars": unit.chars, "text": text, "truncated": truncated,
        }

    def search_source(args):
        query = (args.get("query") or "").strip()
        if not query:
            return {"matches": []}
        emit("search", query=query)
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches = []
        for u in units:
            for m in pattern.finditer(u.text):
                start = max(0, m.start() - 120)
                end = min(len(u.text), m.end() + 120)
                matches.append({"unit_idx": u.idx, "snippet": re.sub(r"\s+", " ", u.text[start:end])})
                if len(matches) >= 12:
                    break
            if len(matches) >= 12:
                break
        return {"matches": matches, "count": len(matches)}

    def spawn_task(args):
        idxs = []
        for raw in args.get("unit_idxs") or []:
            try:
                i = int(raw)
            except (TypeError, ValueError):
                continue
            if i in by_idx and i not in idxs:
                idxs.append(i)
        if not idxs:
            return {"error": "unit_idxs must name at least one existing unit"}
        if len(idxs) > MAX_UNITS_PER_TASK:
            return {"error": f"At most {MAX_UNITS_PER_TASK} units per task; split this task."}
        skipped = [i for i in idxs if i in plan.skips or by_idx[i].skipped]
        if skipped:
            return {"error": f"Units {skipped} are skipped; unskip them first or leave them out."}
        strategy = args.get("strategy") if args.get("strategy") in text_strategies() else DEFAULT_STRATEGY
        try:
            target = int(args.get("target_cards") or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0:
            target = sum(heuristic_target(by_idx[i], strategy) for i in idxs)
        target = max(1, min(MAX_TASK_CARDS, target))
        if len(plan.tasks) >= MAX_TASKS:
            return {"error": f"Task limit ({MAX_TASKS}) reached; call finish_plan."}
        task = PlanTask(id=max([t.id for t in plan.tasks] + [0]) + 1, unit_idxs=sorted(idxs), strategy=strategy,
                        target_cards=target, notes=(args.get("notes") or "")[:1500])
        plan.tasks.append(task)
        emit("spawn", task=task.to_dict())
        return {"task_id": task.id, "ok": True, "tasks_so_far": len(plan.tasks)}

    def skip_unit(args):
        try:
            idx = int(args.get("unit_idx"))
        except (TypeError, ValueError):
            return {"error": "unit_idx must be an integer"}
        if idx not in by_idx:
            return {"error": f"No unit {idx}"}
        plan.skips[idx] = (args.get("reason") or "skipped by planner")[:400]
        # Drop any task that now only covers skipped units.
        plan.tasks[:] = [t for t in plan.tasks if any(i not in plan.skips for i in t.unit_idxs)]
        emit("skip", unit_idx=idx, reason=plan.skips[idx])
        return {"ok": True}

    def _figure_arg(args):
        try:
            return figure_by_number.get(int(args.get("figure")))
        except (TypeError, ValueError):
            return None

    def spawn_figure_task(args):
        figure = _figure_arg(args)
        if figure is None:
            return {"error": f"No figure {args.get('figure')}; use a number from the figure listing."}
        idx = figure["unit_idx"]
        if idx in plan.skips or by_idx[idx].skipped:
            return {"error": f"Figure {figure['number']} sits in unit {idx}, which is skipped."}
        try:
            target = int(args.get("target_cards") or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0:
            return {"error": "target_cards must be at least 1; call skip_figure to give a figure no cards."}
        if len(plan.tasks) >= MAX_TASKS:
            return {"error": f"Task limit ({MAX_TASKS}) reached; call finish_plan."}
        task = _decide_figure(plan, figure, target, notes=args.get("notes") or "")
        emit("spawn", task=task.to_dict())
        return {"task_id": task.id, "ok": True, "tasks_so_far": len(plan.tasks)}

    def skip_figure(args):
        figure = _figure_arg(args)
        if figure is None:
            return {"error": f"No figure {args.get('figure')}; use a number from the figure listing."}
        _decide_figure(plan, figure, 0, reason=args.get("reason") or "")
        emit("skip_figure", figure=figure["number"], reason=plan.figure_skips[figure["id"]])
        return {"ok": True}

    def finish_plan(args):
        plan.summary = (args.get("summary") or "")[:2000]
        uncovered = _uncovered(units, plan)
        if uncovered and not plan.text_tasks:
            return {"error": "No tasks spawned yet. Spawn tasks (or skip units) before finishing.", "uncovered": uncovered}
        raise StopIteration({"ok": True, "uncovered_auto_assigned": uncovered})

    handlers = {
        "read_unit": read_unit,
        "search_source": search_source,
        "spawn_task": spawn_task,
        "skip_unit": skip_unit,
        "spawn_figure_task": spawn_figure_task,
        "skip_figure": skip_figure,
        "finish_plan": finish_plan,
    }

    system = PLANNER_SYSTEM.replace("{catalog}", strategy_catalog()).replace("{max_units}", str(MAX_UNITS_PER_TASK))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _planner_user_message(units, settings, budget, doc_meta, figures)},
    ]

    def chat(transcript, tools):
        result = client.chat("planner", transcript, tools=tools, tool_choice="auto")
        calls.append(result)
        return result.raw

    final_text, transcript, turns, stopped = run_tool_loop(chat, messages, _tools(bool(figures)), handlers,
                                                           max_turns=max_turns)
    plan.turns = turns
    if not plan.tasks and not stopped:
        # The model never engaged with tools: fall back to one structured call.
        logger.warning("Planner produced no tasks in %s turns; using structured fallback", turns)
        structured, result = plan_structured(client, units, settings, doc_meta, budget, figures)
        calls.append(result)
        structured.turns = turns + 1
        return structured, transcript, calls
    if not plan.summary and final_text:
        plan.summary = final_text[:2000]
    _finalize(units, plan, figures)
    return plan, transcript, calls


PLAN_SCHEMA = json_schema_format(
    "work_order",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "skips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"unit_idx": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["unit_idx", "reason"],
                },
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "unit_idxs": {"type": "array", "items": {"type": "integer"}},
                        "strategy": {"type": "string", "enum": list(text_strategies().keys())},
                        "target_cards": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                    "required": ["unit_idxs", "strategy", "target_cards", "notes"],
                },
            },
            "figures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "figure": {"type": "integer"},
                        "target_cards": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                    "required": ["figure", "target_cards", "notes"],
                },
            },
        },
        "required": ["summary", "skips", "tasks", "figures"],
    },
)


def plan_structured(client, units, settings, doc_meta, budget, figures=None):
    """Single-shot fallback: the whole work order as one structured output."""
    system = (
        PLANNER_SYSTEM.replace("{catalog}", strategy_catalog()).replace("{max_units}", str(MAX_UNITS_PER_TASK))
        + "\n\nIn this mode you have no tools: return the complete work order as JSON (tasks, skips, figures, summary). "
        "`figures` holds one entry per listed figure: its number, target_cards (0 to give it no cards) and notes; "
        "leave it empty when no figures are listed."
    )
    figures = list(figures or [])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _planner_user_message(units, settings, budget, doc_meta, figures)},
    ]
    result = client.chat("planner", messages, response_format=PLAN_SCHEMA)
    plan = Plan(budget=budget, mode="structured")
    by_idx = {u.idx: u for u in units}
    try:
        data = extract_json(result.content)
    except Exception:
        data = {}
    for s in data.get("skips") or []:
        try:
            idx = int(s.get("unit_idx"))
        except (TypeError, ValueError):
            continue
        if idx in by_idx:
            plan.skips[idx] = (s.get("reason") or "skipped")[:400]
    next_id = 1
    for t in data.get("tasks") or []:
        idxs = [int(i) for i in (t.get("unit_idxs") or []) if isinstance(i, int) and int(i) in by_idx]
        idxs = [i for i in idxs if i not in plan.skips][:MAX_UNITS_PER_TASK]
        if not idxs:
            continue
        strategy = t.get("strategy") if t.get("strategy") in text_strategies() else DEFAULT_STRATEGY
        target = int(t.get("target_cards") or 0) or sum(heuristic_target(by_idx[i], strategy) for i in idxs)
        plan.tasks.append(PlanTask(id=next_id, unit_idxs=sorted(set(idxs)), strategy=strategy,
                                   target_cards=max(1, min(MAX_TASK_CARDS, target)), notes=(t.get("notes") or "")[:1500]))
        next_id += 1
        if len(plan.tasks) >= MAX_TASKS:
            break
    figure_by_number = {f["number"]: f for f in figures}
    for entry in data.get("figures") or []:
        figure = figure_by_number.get(entry.get("figure")) if isinstance(entry, dict) else None
        if figure is None or len(plan.tasks) >= MAX_TASKS:
            continue
        try:
            target = int(entry.get("target_cards") or 0)
        except (TypeError, ValueError):
            target = 0
        _decide_figure(plan, figure, target, notes=entry.get("notes") or "", reason=entry.get("notes") or "")
    plan.summary = (data.get("summary") or "")[:2000]
    _finalize(units, plan, figures)
    return plan, result


def plan_heuristic(units, budget, figures=None):
    """No-model plan: one general task per live unit, and the vision pass's advice on
    each figure. Used if the planner role fails."""
    plan = Plan(budget=budget, mode="heuristic", summary="Default plan: one general-coverage task per unit.")
    _finalize(units, plan, figures)
    return plan


def _uncovered(units, plan):
    """Live units no text task reads. A figure task covers its figure, not its unit."""
    covered = set()
    for t in plan.text_tasks:
        covered.update(t.unit_idxs)
    return [u.idx for u in units if not u.skipped and u.idx not in plan.skips and u.idx not in covered]


def _finalize(units, plan, figures=None):
    """Enforce invariants: every live unit is covered, every figure is decided, counts
    are bounded."""
    by_idx = {u.idx: u for u in units}
    for u in units:
        if u.skipped and u.idx not in plan.skips:
            plan.skips[u.idx] = u.skip_reason or "skipped by mapper"
    next_id = max([t.id for t in plan.tasks] + [0]) + 1
    for idx in _uncovered(units, plan):
        unit = by_idx[idx]
        plan.tasks.append(
            PlanTask(id=next_id, unit_idxs=[idx], strategy=DEFAULT_STRATEGY,
                     target_cards=heuristic_target(unit), notes="Auto-assigned: the planner left this unit uncovered.",
                     origin="auto")
        )
        next_id += 1
    settle_figures(units, plan, figures)
    plan.tasks = plan.tasks[:MAX_TASKS]
    # A runaway guard, not a budget: the planner may go well over its estimate, and only a
    # plan beyond 2.5x of it is pulled back.
    ceiling = max(20, int(plan.budget * 2.5)) if plan.budget else None
    if ceiling:
        total = sum(t.target_cards for t in plan.tasks)
        if total > ceiling:
            scale = ceiling / float(total)
            for t in plan.tasks:
                t.target_cards = max(1, int(round(t.target_cards * scale)))
    # In document order, a unit's figures after the tasks over its text.
    plan.tasks.sort(key=lambda t: (min(t.unit_idxs), bool(t.figure_id), t.id))
    for i, t in enumerate(plan.tasks, start=1):
        t.id = i
    return plan
