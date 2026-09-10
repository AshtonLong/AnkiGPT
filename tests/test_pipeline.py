"""Unit tests for the pipeline building blocks (no Flask app needed for most)."""

import json

import pytest

from app.services.llm import run_tool_loop, tool_spec
from app.services.pipeline import critic, document_map, planner, reconcile
from app.services.pipeline.cache import make_key
from app.services.pipeline.strategies import STRATEGIES, system_prompt

from conftest import fake_response, tool_call


# ------------------------------------------------------------- document map
class TestSkeleton:
    def test_headings_become_candidates_with_offsets(self):
        text = "# Alpha\n\nBody one.\n\n## Beta\n\nBody two is here.\n\nMore beta."
        cands = document_map.skeleton(text)
        assert [c.title for c in cands] == ["Alpha", "Beta"]
        for c in cands:
            assert text[c.char_start : c.char_end].strip() == c.text

    def test_unstructured_text_is_packed(self):
        text = "\n\n".join("Paragraph %d " % i + "word " * 120 for i in range(12))
        cands = document_map.skeleton(text, max_unit_chars=14000)
        assert len(cands) > 1

    def test_oversized_section_is_split(self):
        text = "# Big\n\n" + "\n\n".join("para " * 200 for _ in range(30))
        cands = document_map.skeleton(text, max_unit_chars=5000)
        assert len(cands) > 1
        assert all(c.chars <= 5200 for c in cands)
        assert cands[0].title.startswith("Big (1/")

    def test_page_assignment(self):
        units = [document_map.Unit(idx=0, title="a", text="x", char_start=0, char_end=100),
                 document_map.Unit(idx=1, title="b", text="y", char_start=100, char_end=300)]
        document_map.assign_pages(units, [[1, 0], [2, 120], [3, 250]])
        assert (units[0].page_start, units[0].page_end) == (1, 1)
        assert (units[1].page_start, units[1].page_end) == (1, 3)


class TestAssembleUnits:
    def test_repairs_missing_and_duplicate_ids(self):
        cands = document_map.skeleton("# A\n\none\n\n# B\n\ntwo\n\n# C\n\nthree")
        mapping = {"units": [
            {"title": "AB", "candidate_ids": [0, 1, 1], "kind": "prose", "density": 3, "depends_on": [], "skip": False,
             "skip_reason": None, "summary": "s"},
            # candidate 2 forgotten; bogus id 9 ignored
            {"title": "junk", "candidate_ids": [9], "kind": "prose", "density": 3, "depends_on": [0], "skip": False,
             "skip_reason": None, "summary": ""},
        ]}
        units = document_map._assemble_units(cands, mapping, 14000)
        assert [u.title for u in units] == ["AB", "C"]
        assert units[0].text == "one\n\ntwo"

    def test_depends_on_is_remapped_after_split(self):
        cands = document_map.skeleton("# A\n\n" + "\n\n".join("para " * 150 for _ in range(6)) + "\n\n# B\n\nshort")
        mapping = {"units": [
            {"title": "A", "candidate_ids": [0], "kind": "prose", "density": 3, "depends_on": [], "skip": False, "skip_reason": None, "summary": ""},
            {"title": "B", "candidate_ids": [1], "kind": "prose", "density": 3, "depends_on": [0], "skip": False, "skip_reason": None, "summary": ""},
        ]}
        units = document_map._assemble_units(cands, mapping, 2000)
        assert len(units) >= 3  # A split into pieces + B
        assert units[-1].title == "B"
        assert units[-1].depends_on == [0]


# ------------------------------------------------------------------ planner
def _units(n=3, skipped=()):
    out = []
    for i in range(n):
        u = document_map.Unit(idx=i, title=f"U{i}", text="word " * 400, char_start=0, char_end=2000, density=3)
        if i in skipped:
            u.skipped = True
            u.skip_reason = "fluff"
        out.append(u)
    return out


class TestPlannerInvariants:
    def test_uncovered_units_get_auto_tasks(self):
        units = _units(3, skipped=(2,))
        plan = planner.Plan(tasks=[planner.PlanTask(1, [0], "general", 5)], budget=20)
        planner._finalize(units, plan)
        covered = {i for t in plan.tasks for i in t.unit_idxs}
        assert covered == {0, 1}
        assert any(t.origin == "auto" for t in plan.tasks)
        assert plan.skips == {2: "fluff"}

    def test_runaway_budget_is_scaled(self):
        units = _units(2)
        plan = planner.Plan(tasks=[planner.PlanTask(1, [0], "general", 60), planner.PlanTask(2, [1], "general", 60)], budget=20)
        planner._finalize(units, plan)
        assert sum(t.target_cards for t in plan.tasks) <= 50

    def test_heuristic_target_scales_with_density(self):
        low = document_map.Unit(idx=0, title="", text="w" * 4000, char_start=0, char_end=0, density=1)
        high = document_map.Unit(idx=1, title="", text="w" * 4000, char_start=0, char_end=0, density=5)
        assert planner.heuristic_target(low) < planner.heuristic_target(high)

    def test_plan_roundtrip(self):
        plan = planner.Plan(tasks=[planner.PlanTask(1, [0, 1], "compare_contrast", 7, "notes", "planner")], skips={3: "x"},
                            summary="s", budget=9, turns=2, mode="agent")
        again = planner.Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert again.to_dict() == plan.to_dict()


class FakeClient:
    """Minimal stand-in for LLMClient.chat used by the planner loop."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, role, messages, **kwargs):
        from app.services.pipeline.routing import ChatResult

        self.calls += 1
        raw = self.script.pop(0)
        msg = raw["choices"][0]["message"]
        return ChatResult(content=msg.get("content") or "", message=msg, usage=raw["usage"], model="m", raw=raw)


def test_planner_agent_loop_spawns_and_validates():
    units = _units(3)
    client = FakeClient([
        fake_response(None, tool_calls=[tool_call("1", "read_unit", {"unit_idx": 1}),
                                        tool_call("2", "search_source", {"query": "word"})]),
        fake_response(None, tool_calls=[
            tool_call("3", "spawn_task", {"unit_idxs": [0, 1], "strategy": "mechanism_chain", "target_cards": 8, "notes": "n"}),
            tool_call("4", "spawn_task", {"unit_idxs": [7], "strategy": "general", "target_cards": 3, "notes": ""}),  # bad idx
            tool_call("5", "skip_unit", {"unit_idx": 2, "reason": "recap"}),
        ]),
        fake_response(None, tool_calls=[tool_call("6", "finish_plan", {"summary": "done"})]),
    ])
    plan, transcript, calls = planner.plan_with_agent(client, units, {}, {}, budget=20, max_turns=6)
    assert plan.mode == "agent"
    assert plan.summary == "done"
    assert [t.unit_idxs for t in plan.tasks] == [[0, 1]]
    assert plan.tasks[0].strategy == "mechanism_chain"
    assert plan.skips == {2: "recap"}
    assert client.calls == 3
    # The bad spawn returned an error to the model rather than crashing.
    tool_msgs = [m for m in transcript if m["role"] == "tool"]
    assert any("error" in m["content"] for m in tool_msgs)


def test_planner_falls_back_to_structured_when_no_tools_used():
    units = _units(2)
    structured = json.dumps({"summary": "fallback", "skips": [], "tasks": [
        {"unit_idxs": [0], "strategy": "definition_sweep", "target_cards": 4, "notes": ""},
    ]})
    client = FakeClient([fake_response("I would rather not."), fake_response(structured)])
    plan, _t, _c = planner.plan_with_agent(client, units, {}, {}, budget=10, max_turns=3)
    assert plan.mode == "structured"
    assert {t.strategy for t in plan.tasks} == {"definition_sweep", "general"}  # unit 1 auto-covered


# ----------------------------------------------------------------- tool loop
def test_run_tool_loop_stops_on_stop_iteration():
    seen = []

    def chat(messages, tools):
        seen.append(len(messages))
        if len(seen) == 1:
            return fake_response(None, tool_calls=[tool_call("a", "ping", {"x": 1}), tool_call("b", "done", {})])
        return fake_response("should not be reached")

    def done(args):
        raise StopIteration({"finished": True})

    text, transcript, turns, stopped = run_tool_loop(
        chat, [{"role": "user", "content": "go"}], [tool_spec("ping", "", {}), tool_spec("done", "", {})],
        {"ping": lambda a: {"pong": a["x"]}, "done": done},
    )
    assert stopped and turns == 1
    tool_msgs = [m for m in transcript if m["role"] == "tool"]
    assert json.loads(tool_msgs[0]["content"]) == {"pong": 1}
    assert json.loads(tool_msgs[1]["content"]) == {"finished": True}


# ------------------------------------------------------------------- critic
class TestCritic:
    def test_prompt_masks_cloze(self):
        assert critic.card_prompt({"type": "cloze", "cloze_text": "ATP is made in the {{c1::mitochondria}}."}) == "ATP is made in the [...]."
        assert critic.card_answer({"type": "cloze", "cloze_text": "{{c1::A}} and {{c2::B::hint}}"}) == "A / B"

    def test_drop_marks_deleted_with_reason_tags(self):
        card = {"type": "basic", "front": "q", "back": "a"}
        out, status, tags = critic.apply_verdict(card, {"verdict": "drop", "supported": False, "reason": "made up", "difficulty": 2})
        assert status == "deleted"
        assert "critic:unsupported" in tags and "critic:dropped" in tags
        assert out["difficulty"] == 2

    def test_rewrite_replaces_fields(self):
        card = {"type": "basic", "front": "vague?", "back": "a"}
        verdict = {"verdict": "rewrite", "difficulty": 3, "rewrite": {"type": "cloze", "front": None, "back": None,
                   "cloze_text": "The answer is {{c1::a}}.", "extra": ""}}
        out, status, tags = critic.apply_verdict(card, verdict)
        assert status == "ok" and out["type"] == "cloze" and "critic:rewritten" in tags

    def test_broken_rewrite_flags_for_review(self):
        card = {"type": "cloze", "cloze_text": "x {{c1::y}}"}
        verdict = {"verdict": "rewrite", "rewrite": {"type": "cloze", "cloze_text": "no deletion", "front": None, "back": None, "extra": None}}
        _out, status, tags = critic.apply_verdict(card, verdict)
        assert status == "needs_review" and "critic:needs_review" in tags


# ---------------------------------------------------------------- reconcile
class TestReconcile:
    def test_clusters_by_cosine(self):
        vectors = [[1, 0, 0], [0.99, 0.05, 0], [0, 1, 0], [0, 0, 1], [0, 0.98, 0.1]]
        clusters = reconcile.cluster_by_similarity(vectors, threshold=0.9)
        assert clusters == [[0, 1], [2, 4]]

    def test_exact_duplicates(self):
        cards = [{"type": "basic", "front": "What is X?", "back": "Y"},
                 {"type": "basic", "front": "what is x?", "back": "y"},
                 {"type": "basic", "front": "Other", "back": "z"}]
        assert reconcile.exact_duplicate_indices(cards) == [1]


# ----------------------------------------------------------------- strategies
def test_every_strategy_prompt_shares_the_base_prefix():
    prompts = [system_prompt(k, "basic") for k in STRATEGIES]
    base = prompts[0].split("\n\nSTRATEGY:")[0]
    assert all(p.startswith(base) for p in prompts)
    assert "source_quote" in base


def test_cache_key_is_stable_and_input_sensitive():
    a = make_key("worker", "m", "v", [{"role": "user", "content": "x"}])
    b = make_key("worker", "m", "v", [{"role": "user", "content": "x"}])
    c = make_key("worker", "m", "v", [{"role": "user", "content": "y"}])
    assert a == b != c and len(a) == 64
