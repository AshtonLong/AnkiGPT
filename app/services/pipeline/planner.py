"""Phase 1 — the planner.

The planner is a real agent loop: it sees the document map, can read any unit in full,
can search the source, and spawns worker tasks with a strategy, a card budget, and
notes. It decides how the material is carved up — the thing the old pipeline hard-coded
as `max_chars=3500`.

Safety rails: every non-skipped unit must end up covered (uncovered units get a default
task), task and card counts are capped, and if the model never calls a tool we fall back
to a single structured-output planning call.
"""

import logging
import re
from dataclasses import dataclass, field

from ..llm import extract_json, json_schema_format, run_tool_loop, tool_spec
from .strategies import DEFAULT_STRATEGY, STRATEGIES, get_strategy, strategy_catalog

logger = logging.getLogger(__name__)

PLAN_PROMPT_VERSION = "plan-v1"

MAX_TASKS = 80
MAX_UNITS_PER_TASK = 4
READ_UNIT_CAP = 9000


@dataclass
class PlanTask:
    id: int
    unit_idxs: list
    strategy: str
    target_cards: int
    notes: str = ""
    origin: str = "planner"  # planner | auto | coverage | figure
    figure_id: int = None

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
    summary: str = ""
    budget: int = 0
    turns: int = 0
    mode: str = "agent"  # agent | structured | heuristic

    def to_dict(self):
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "skips": {str(k): v for k, v in self.skips.items()},
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
            summary=d.get("summary") or "",
            budget=int(d.get("budget") or 0),
            turns=int(d.get("turns") or 0),
            mode=d.get("mode") or "agent",
        )
        return plan


# ----------------------------------------------------------------------------- sizing
def heuristic_target(unit, strategy_key=DEFAULT_STRATEGY):
    """Cards a unit 'deserves' from its size and density; the planner's starting point."""
    strategy = get_strategy(strategy_key)
    density_factor = {1: 0.35, 2: 0.6, 3: 1.0, 4: 1.35, 5: 1.7}.get(int(unit.density or 3), 1.0)
    raw = (unit.chars / 1000.0) * strategy.cards_per_1k_chars * density_factor
    return max(2, min(45, int(round(raw))))


def suggest_budget(units, requested=None):
    live = [u for u in units if not u.skipped]
    auto = sum(heuristic_target(u) for u in live)
    if requested and int(requested) > 0:
        return max(5, int(requested))
    return max(5, auto)


# ------------------------------------------------------------------------------ prompt
PLANNER_SYSTEM = """You are the planning agent of a flashcard generator. Your job is to decide HOW a study document is turned into Anki cards, then delegate the writing to worker agents by spawning tasks. You do not write cards yourself.

You have tools:
- read_unit(unit_idx): read a unit's full text. Use it whenever the summary is not enough to choose a strategy or a card budget.
- search_source(query): find where a term or topic appears across units.
- spawn_task(unit_idxs, strategy, target_cards, notes): delegate a card-writing task. One task = one worker call that reads the listed units verbatim. Group only adjacent, closely related units (max {max_units} per task). You may spawn several tasks with different strategies over the same unit when it deserves it (e.g. definition_sweep + pitfall_edge_case).
- skip_unit(unit_idx, reason): exclude a unit entirely (fluff, references, recap of earlier units, out of the student's focus).
- finish_plan(summary): call this exactly once when every non-skipped unit is covered by at least one task.

Strategies (choose per task):
{catalog}

Planning principles
- Read first, then decide. Summaries can mislead; read any unit whose kind or density you are unsure about, and always read units marked figure_heavy or worked_example before assigning a strategy.
- Match the strategy to the unit's real structure. Mixed units get `general`; dense glossaries get `definition_sweep`; equation-heavy text gets `formula_derivation`; processes get `mechanism_chain`; confusable siblings get `compare_contrast`.
- Size the budget to the content: `target_cards` is the number of high-value cards the unit can honestly support. Dense units earn more; narrative earns fewer. Stay near the suggested overall budget (within about 30%). Never pad.
- Notes are instructions to the worker. Make them specific and local: "one card per symbol in the rate-law table", "contrast the three isotherm models on their assumptions", "ignore the historical aside". Mention the student's focus/exclusions where relevant.
- Respect the student's focus and exclusions. Skip what they excluded; weight what they asked for.
- Do not spawn tasks for skipped units. Do not duplicate coverage across tasks unless the strategies differ.
- Figures are handled by a separate vision pass; do not plan for them.
- Be decisive: aim to finish in as few turns as possible while still reading what you need."""


def _unit_listing(units, figure_counts=None):
    lines = []
    for u in units:
        flags = []
        if u.skipped:
            flags.append(f"SKIPPED by mapper: {u.skip_reason or 'n/a'}")
        if figure_counts and figure_counts.get(u.idx):
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


def _planner_user_message(units, settings, budget, doc_meta, figure_counts=None):
    focus = settings.get("focus") or ""
    exclude = settings.get("exclude") or ""
    glossary = settings.get("glossary") or ""
    context = settings.get("exam_context") or ""
    card_style = settings.get("card_style") or "auto"
    live = [u for u in units if not u.skipped]
    return "\n".join(
        [
            f"Subject: {doc_meta.get('subject') or 'unknown'}",
            f"Document summary: {doc_meta.get('document_summary') or '(none)'}",
            f"Student context / exam: {context or 'not given'}",
            f"Preferred card style: {card_style}",
            f"Focus: {focus or 'all exam-useful material'}",
            f"Exclude: {exclude or 'none'}",
            f"Must-include terms: {glossary or 'none'}",
            f"Suggested overall budget: about {budget} cards across {len(live)} live units.",
            "",
            f"Document map ({len(units)} units):",
            _unit_listing(units, figure_counts),
            "",
            "Plan the work. Read what you need, spawn tasks, skip what deserves skipping, then call finish_plan.",
        ]
    )


def _tools():
    return [
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
                    "strategy": {"type": "string", "enum": list(STRATEGIES.keys())},
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
        tool_spec(
            "finish_plan",
            "Finish planning. Call once every non-skipped unit is covered.",
            {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Two to four sentences explaining the plan to the student."}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        ),
    ]


# --------------------------------------------------------------------------- planning
def plan_with_agent(client, units, settings, doc_meta, budget, max_turns=14, figure_counts=None, on_event=None):
    """Run the planner loop. Returns (Plan, transcript, ChatResults list)."""
    by_idx = {u.idx: u for u in units}
    plan = Plan(budget=budget)
    calls = []
    next_id = [1]

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
        strategy = args.get("strategy") if args.get("strategy") in STRATEGIES else DEFAULT_STRATEGY
        try:
            target = int(args.get("target_cards") or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0:
            target = sum(heuristic_target(by_idx[i], strategy) for i in idxs)
        target = max(1, min(60, target))
        if len(plan.tasks) >= MAX_TASKS:
            return {"error": f"Task limit ({MAX_TASKS}) reached; call finish_plan."}
        task = PlanTask(id=next_id[0], unit_idxs=sorted(idxs), strategy=strategy, target_cards=target,
                        notes=(args.get("notes") or "")[:1500])
        next_id[0] += 1
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

    def finish_plan(args):
        plan.summary = (args.get("summary") or "")[:2000]
        uncovered = _uncovered(units, plan)
        if uncovered and not plan.tasks:
            return {"error": "No tasks spawned yet. Spawn tasks (or skip units) before finishing.", "uncovered": uncovered}
        raise StopIteration({"ok": True, "uncovered_auto_assigned": uncovered})

    handlers = {
        "read_unit": read_unit,
        "search_source": search_source,
        "spawn_task": spawn_task,
        "skip_unit": skip_unit,
        "finish_plan": finish_plan,
    }

    system = PLANNER_SYSTEM.replace("{catalog}", strategy_catalog()).replace("{max_units}", str(MAX_UNITS_PER_TASK))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _planner_user_message(units, settings, budget, doc_meta, figure_counts)},
    ]

    def chat(transcript, tools):
        result = client.chat("planner", transcript, tools=tools, tool_choice="auto")
        calls.append(result)
        return result.raw

    final_text, transcript, turns, stopped = run_tool_loop(chat, messages, _tools(), handlers, max_turns=max_turns)
    plan.turns = turns
    if not plan.tasks and not stopped:
        # The model never engaged with tools: fall back to one structured call.
        logger.warning("Planner produced no tasks in %s turns; using structured fallback", turns)
        structured, result = plan_structured(client, units, settings, doc_meta, budget, figure_counts)
        calls.append(result)
        structured.turns = turns + 1
        return structured, transcript, calls
    if not plan.summary and final_text:
        plan.summary = final_text[:2000]
    _finalize(units, plan)
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
                        "strategy": {"type": "string", "enum": list(STRATEGIES.keys())},
                        "target_cards": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                    "required": ["unit_idxs", "strategy", "target_cards", "notes"],
                },
            },
        },
        "required": ["summary", "skips", "tasks"],
    },
)


def plan_structured(client, units, settings, doc_meta, budget, figure_counts=None):
    """Single-shot fallback: the whole work order as one structured output."""
    system = (
        PLANNER_SYSTEM.replace("{catalog}", strategy_catalog()).replace("{max_units}", str(MAX_UNITS_PER_TASK))
        + "\n\nIn this mode you have no tools: return the complete work order as JSON (tasks, skips, summary)."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _planner_user_message(units, settings, budget, doc_meta, figure_counts)},
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
        strategy = t.get("strategy") if t.get("strategy") in STRATEGIES else DEFAULT_STRATEGY
        target = int(t.get("target_cards") or 0) or sum(heuristic_target(by_idx[i], strategy) for i in idxs)
        plan.tasks.append(PlanTask(id=next_id, unit_idxs=sorted(set(idxs)), strategy=strategy,
                                   target_cards=max(1, min(60, target)), notes=(t.get("notes") or "")[:1500]))
        next_id += 1
        if len(plan.tasks) >= MAX_TASKS:
            break
    plan.summary = (data.get("summary") or "")[:2000]
    _finalize(units, plan)
    return plan, result


def plan_heuristic(units, budget):
    """No-model plan: one general task per live unit. Used if the planner role fails."""
    plan = Plan(budget=budget, mode="heuristic", summary="Default plan: one general-coverage task per unit.")
    _finalize(units, plan)
    return plan


def _uncovered(units, plan):
    covered = set()
    for t in plan.tasks:
        covered.update(t.unit_idxs)
    return [u.idx for u in units if not u.skipped and u.idx not in plan.skips and u.idx not in covered]


def _finalize(units, plan):
    """Enforce invariants: every live unit is covered, counts are bounded."""
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
    plan.tasks = plan.tasks[:MAX_TASKS]
    # Hard ceiling on total cards: 2.5x the budget stops a runaway planner.
    ceiling = max(20, int(plan.budget * 2.5)) if plan.budget else None
    if ceiling:
        total = sum(t.target_cards for t in plan.tasks)
        if total > ceiling:
            scale = ceiling / float(total)
            for t in plan.tasks:
                t.target_cards = max(1, int(round(t.target_cards * scale)))
    plan.tasks.sort(key=lambda t: (min(t.unit_idxs), t.id))
    for i, t in enumerate(plan.tasks, start=1):
        t.id = i
    return plan
