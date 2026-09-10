"""Run trace: the tree of phases and tasks the status page renders live.

All methods are main-thread only (they touch the DB session). Worker threads return
plain results; the orchestrator calls into the tracer as those results land.
"""

import logging

from ...extensions import db
from ...models import LLMRun, PipelineTask, utcnow

logger = logging.getLogger(__name__)

PHASES = [
    ("map", "Map"),
    ("plan", "Plan"),
    ("figures", "Figures"),
    ("write", "Write"),
    ("critique", "Critique"),
    ("reconcile", "Reconcile"),
    ("coverage", "Coverage"),
    ("finish", "Finish"),
]
PHASE_LABELS = dict(PHASES)


class Tracer:
    def __init__(self, deck):
        self.deck = deck
        self.deck_id = deck.id
        self._seq = 0
        self.phase_nodes = {}
        self.totals = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "calls": 0, "cached": 0}

    # ----------------------------------------------------------------- run state
    def set_run(self, **updates):
        run = dict(self.deck.run_json or {})
        run.update(updates)
        run["totals"] = dict(self.totals)
        self.deck.run_json = run
        db.session.commit()

    def get_run(self):
        return dict(self.deck.run_json or {})

    def clear(self):
        PipelineTask.query.filter_by(deck_id=self.deck_id).delete()
        db.session.commit()

    # -------------------------------------------------------------------- nodes
    def _next_seq(self):
        self._seq += 1
        return self._seq

    def phase(self, phase, label=None, status="running"):
        node = PipelineTask(
            deck_id=self.deck_id,
            seq=self._next_seq(),
            phase=phase,
            kind="phase",
            label=label or PHASE_LABELS.get(phase, phase),
            status=status,
            started_at=utcnow() if status == "running" else None,
        )
        db.session.add(node)
        db.session.commit()
        self.phase_nodes[phase] = node
        self.set_run(phase=phase)
        return node

    def task(self, phase, label, strategy=None, unit_ids=None, model=None, target_cards=None, notes=None,
             status="queued", parent=None, kind="task", result=None):
        parent_node = parent or self.phase_nodes.get(phase)
        node = PipelineTask(
            deck_id=self.deck_id,
            parent_id=parent_node.id if parent_node else None,
            seq=self._next_seq(),
            phase=phase,
            kind=kind,
            label=label,
            strategy=strategy,
            unit_ids=list(unit_ids or []),
            model=model,
            target_cards=target_cards,
            notes=notes,
            status=status,
            result_json=result,
        )
        db.session.add(node)
        db.session.commit()
        return node

    def start(self, node):
        node.status = "running"
        node.started_at = utcnow()
        db.session.commit()

    def finish(self, node, status="done", cards_made=None, cards_kept=None, error=None, result=None, usage=None,
               cached=False):
        node.status = status
        node.finished_at = utcnow()
        if cards_made is not None:
            node.cards_made = cards_made
        if cards_kept is not None:
            node.cards_kept = cards_kept
        if error:
            node.error = str(error)[:2000]
        if result is not None:
            node.result_json = result
        if usage:
            self._apply_usage(node, usage, cached=cached)
        db.session.commit()

    def _apply_usage(self, node, usage, cached=False):
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            cost = usage.get("total_cost")
        try:
            cost = float(cost or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if cached:
            # A cache hit costs nothing now; keep the tokens for reference but not the cost.
            self.totals["cached"] += 1
            cost = 0.0
        else:
            self.totals["calls"] += 1
            self.totals["input_tokens"] += in_tok
            self.totals["output_tokens"] += out_tok
            self.totals["cost"] += cost
        node.input_tokens = (node.input_tokens or 0) + in_tok
        node.output_tokens = (node.output_tokens or 0) + out_tok
        node.cost = (node.cost or 0.0) + cost
        # Roll up into the phase node so the phase row shows its own totals.
        if node.parent_id:
            parent = db.session.get(PipelineTask, node.parent_id)
            if parent is not None:
                parent.input_tokens = (parent.input_tokens or 0) + in_tok
                parent.output_tokens = (parent.output_tokens or 0) + out_tok
                parent.cost = (parent.cost or 0.0) + cost

    def end_phase(self, phase, status="done", error=None, result=None):
        node = self.phase_nodes.get(phase)
        if node is None:
            return
        node.status = status
        node.finished_at = utcnow()
        if error:
            node.error = str(error)[:2000]
        if result is not None:
            node.result_json = result
        db.session.commit()
        self.set_run()

    # ------------------------------------------------------------------ llm log
    def log_call(self, node, role, result, messages=None, prompt_version=None, parsed=None, source_id=None,
                 error=None, cached=False):
        """Persist one LLM call under a task node and fold its usage into the totals."""
        usage = result.usage if result is not None else {}
        run = LLMRun(
            deck_id=self.deck_id,
            source_id=source_id,
            task_id=node.id if node is not None else None,
            role=role,
            model=(result.model if result is not None else None) or (node.model if node else None),
            prompt_version=prompt_version,
            input_tokens=int(usage.get("prompt_tokens") or 0) if usage else None,
            output_tokens=int(usage.get("completion_tokens") or 0) if usage else None,
            cost_estimate=(result.cost if result is not None else None),
            cached=cached,
            request_json={"messages": _trim_messages(messages)} if messages else None,
            response_text=(result.content if result is not None else None),
            parsed_json=parsed,
            error=str(error)[:2000] if error else None,
        )
        db.session.add(run)
        if node is not None and usage:
            self._apply_usage(node, usage, cached=cached)
        db.session.commit()


def _trim_messages(messages, limit=12000):
    """Keep request logs useful but bounded: long unit texts are truncated."""
    trimmed = []
    for msg in messages or []:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str) and len(content) > limit:
            m["content"] = content[:limit] + f"\n...[truncated {len(content) - limit} chars]"
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "image_url", "image_url": {"url": "<image omitted>"}})
                elif isinstance(part, dict) and isinstance(part.get("text"), str) and len(part["text"]) > limit:
                    parts.append({"type": "text", "text": part["text"][:limit] + "...[truncated]"})
                else:
                    parts.append(part)
            m["content"] = parts
        trimmed.append(m)
    return trimmed
