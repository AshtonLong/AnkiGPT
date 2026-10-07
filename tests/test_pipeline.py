"""Unit tests for the pipeline building blocks (no Flask app needed for most)."""

import json

import pytest

from app.services.llm import run_tool_loop, tool_spec
from app.services.pipeline import cheatsheet, critic, document_map, planner, reconcile
from app.services.pipeline.cache import make_key
from app.services.pipeline.strategies import STRATEGIES, system_prompt
from app.services.pipeline.trace import PHASES, phases_for

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


# ------------------------------------------------------- figures in the plan
def _figures():
    """Two pictures in unit 0, one in unit 2; the vision pass thinks the middle one adds nothing."""
    return [
        {"id": 11, "number": 1, "unit_idx": 0, "page": 3, "kind": "diagram", "caption": "Cell membrane",
         "adds": "Shows where each protein sits.", "suggested": 2},
        {"id": 12, "number": 2, "unit_idx": 0, "page": 4, "kind": "chart", "caption": "Rate against time",
         "adds": "Nothing the text does not state.", "suggested": 0},
        {"id": 13, "number": 3, "unit_idx": 2, "page": 9, "kind": "diagram", "caption": "Krebs cycle", "adds": "", "suggested": 3},
    ]


class TestFiguresInThePlan:
    def test_planner_is_shown_each_figure_and_given_the_tools(self):
        units = _units(3)
        user = planner._planner_user_message(units, {}, 30, {}, _figures())
        assert "Figures (3), each read from its page image by a vision pass:" in user
        assert "F1: unit 0 · p.3 · diagram · vision suggests 2 card(s)\n    caption: Cell membrane\n" in user
        assert "adds beyond the text: Nothing the text does not state." in user
        assert "· 2 figure(s)" in user and "decide every figure" in user
        names = lambda tools: [t["function"]["name"] for t in tools]  # noqa: E731
        assert {"spawn_figure_task", "skip_figure"} <= set(names(planner._tools(True)))
        # A deck with no figures is planned exactly as before.
        assert "Figures" not in planner._planner_user_message(units, {}, 30, {})
        assert names(planner._tools()) == ["read_unit", "search_source", "spawn_task", "skip_unit", "finish_plan"]
        # figure_recall is what a figure task runs; it is not offered for a task over text.
        spawn = next(t for t in planner._tools() if t["function"]["name"] == "spawn_task")
        assert "figure_recall" not in spawn["function"]["parameters"]["properties"]["strategy"]["enum"]
        assert "figure_recall" not in planner.strategy_catalog()

    def test_planner_decides_each_figure(self):
        units = _units(3)
        client = FakeClient([
            fake_response(None, tool_calls=[
                tool_call("1", "spawn_task", {"unit_idxs": [0, 1, 2], "strategy": "general", "target_cards": 9, "notes": ""}),
                tool_call("2", "spawn_figure_task", {"figure": 1, "target_cards": 1, "notes": "Only the channel proteins."}),
                tool_call("3", "skip_figure", {"figure": 3, "reason": "Repeats figure 1."}),
                tool_call("4", "spawn_figure_task", {"figure": 8, "target_cards": 2, "notes": ""}),  # no such figure
                tool_call("5", "spawn_figure_task", {"figure": 2, "target_cards": 0, "notes": ""}),  # zero is a skip
            ]),
            fake_response(None, tool_calls=[tool_call("6", "finish_plan", {"summary": "done"})]),
        ])
        plan, transcript, _calls = planner.plan_with_agent(client, units, {}, {}, budget=20, figures=_figures())
        figure_task, = plan.figure_tasks
        assert (figure_task.figure_id, figure_task.unit_idxs, figure_task.strategy) == (11, [0], "figure_recall")
        assert (figure_task.target_cards, figure_task.notes, figure_task.origin) == (1, "Only the channel proteins.", "planner")
        # The planner's skip stands; the figure it left undecided follows the vision pass, which wanted none.
        assert plan.figure_skips[13] == "Repeats figure 1."
        assert "nothing in it worth a card" in plan.figure_skips[12]
        errors = [m["content"] for m in transcript if m["role"] == "tool" and "error" in m["content"]]
        assert len(errors) == 2 and "No figure 8" in errors[0] and "skip_figure" in errors[1]
        # In document order, a unit's figures after its text.
        assert [bool(t.figure_id) for t in plan.tasks] == [False, True]
        assert planner.Plan.from_dict(json.loads(json.dumps(plan.to_dict()))).to_dict() == plan.to_dict()

    def test_an_undecided_figure_follows_the_vision_pass(self):
        units = _units(3, skipped=(2,))
        plan = planner.plan_heuristic(units, 20, _figures())
        auto, = plan.figure_tasks
        assert (auto.figure_id, auto.target_cards, auto.origin) == (11, 2, "auto")
        # Nothing is written from a skipped unit, its figure included.
        assert plan.figure_skips[13] == "Its unit is skipped." and 12 in plan.figure_skips

    def test_a_ruling_can_be_changed_and_dies_with_its_unit(self):
        units = _units(2)
        figures = _figures()[:1]
        client = FakeClient([
            fake_response(None, tool_calls=[
                tool_call("1", "spawn_task", {"unit_idxs": [1], "strategy": "general", "target_cards": 4, "notes": ""}),
                tool_call("2", "skip_figure", {"figure": 1, "reason": "No."}),
                tool_call("3", "spawn_figure_task", {"figure": 1, "target_cards": 30, "notes": ""}),
                tool_call("4", "skip_unit", {"unit_idx": 0, "reason": "recap"}),
                tool_call("5", "spawn_figure_task", {"figure": 1, "target_cards": 2, "notes": ""}),
            ]),
            fake_response(None, tool_calls=[tool_call("6", "finish_plan", {"summary": "done"})]),
        ])
        plan, transcript, _calls = planner.plan_with_agent(client, units, {}, {}, budget=20, figures=figures)
        assert not plan.figure_tasks and plan.figure_skips == {11: "Its unit is skipped."}
        assert any("which is skipped" in m["content"] for m in transcript if m["role"] == "tool")

    def test_a_figure_task_does_not_cover_its_unit(self):
        units = _units(1)
        plan = planner.Plan(budget=20)
        planner._decide_figure(plan, _figures()[0], 2)
        planner._finalize(units, plan, _figures()[:1])
        text, figure = plan.tasks
        assert (text.origin, text.figure_id) == ("auto", None) and figure.figure_id == 11

    def test_structured_fallback_plans_figures_too(self):
        units = _units(1)
        structured = json.dumps({"summary": "fallback", "skips": [], "tasks": [
            {"unit_idxs": [0], "strategy": "definition_sweep", "target_cards": 4, "notes": ""},
        ], "figures": [{"figure": 1, "target_cards": 3, "notes": "labels"}, {"figure": 2, "target_cards": 0, "notes": "in the text"}]})
        client = FakeClient([fake_response("I would rather not."), fake_response(structured)])
        plan, _t, _c = planner.plan_with_agent(client, units, {}, {}, budget=10, max_turns=3, figures=_figures()[:2])
        assert plan.mode == "structured"
        assert [(t.figure_id, t.target_cards, t.notes) for t in plan.figure_tasks] == [(11, 3, "labels")]
        assert plan.figure_skips == {12: "in the text"}

    def test_the_estimate_counts_figures_and_is_not_a_cap(self):
        units = _units(2)
        text_only = planner.suggest_budget(units)
        assert planner.suggest_budget(units, figures=_figures()) == text_only + 2  # the figure in unit 2 has no unit here
        # A number the student typed stands for the whole deck.
        assert planner.suggest_budget(units, 40, _figures()) == 40
        auto = planner._planner_user_message(units, {}, text_only, {})
        assert f"about {text_only} cards" in auto and "not a limit" in auto
        asked = planner._planner_user_message(units, {"target_cards": 40}, 40, {})
        assert "The student asked for about 40 cards" in asked and "not a limit" not in asked
        assert "estimate, not a limit" in planner.PLANNER_SYSTEM
        assert planner.requested_cards({"target_cards": "auto"}) is None and planner.requested_cards({"target_cards": "25"}) == 25

    def test_a_diagram_line_on_the_sheet_is_not_sized_as_text(self):
        body = "- A fact about membranes.\n" * 40
        plain = document_map.Unit(idx=0, title="", text=body, char_start=0, char_end=0, density=5)
        with_diagrams = document_map.Unit(idx=0, title="", text=body + "[[Figure 1]] A long caption about the cell.\n" * 20,
                                          char_start=0, char_end=0, density=5)
        assert planner.heuristic_target(with_diagrams) == planner.heuristic_target(plain)


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

    def test_judge_rules_on_whether_a_card_is_worth_learning(self):
        schema = critic.JUDGE_SCHEMA["json_schema"]["schema"]["properties"]["verdicts"]["items"]
        assert "worthwhile" in schema["required"]
        assert "worthwhile" in critic.JUDGE_SYSTEM and "not worthwhile" in critic.JUDGE_SYSTEM
        card = {"type": "basic", "front": "In this automaton, where does the a-transition from state 6 lead?", "back": "State 5"}
        _out, status, tags = critic.apply_verdict(card, {"verdict": "drop", "supported": True, "worthwhile": False})
        assert status == "deleted" and "critic:not_worthwhile" in tags and "critic:unsupported" not in tags
        # A verdict from before the judge was asked says nothing against the card.
        assert "critic:not_worthwhile" not in critic.apply_verdict(card, {"verdict": "drop", "supported": False})[2]

    def test_a_card_is_listed_as_question_and_answer(self):
        assert critic.card_line({"type": "basic", "front": "What is  X?", "back": "Y\nand Z"}) == "What is X? -> Y and Z"
        assert critic.card_line({"type": "cloze", "cloze_text": "ATP is made in the {{c1::mitochondria}}."}) == (
            "ATP is made in the [...]. -> mitochondria")


# ------------------------------------------------------ coverage and back-fill
class TestBackfill:
    def _unit(self):
        return document_map.Unit(idx=0, title="Cells", text="[[Figure 1]] A cell\n- ATP is made in the mitochondria.",
                                 char_start=0, char_end=0)

    def test_audit_reads_answers_and_ignores_diagram_lines(self):
        system, user = [m["content"] for m in reconcile.coverage_messages(self._unit(), ["Where is ATP made? -> mitochondria"])]
        assert "Read the answers" in system and "[[Figure N]]" in system and "empty list is the right answer" in system
        assert user.endswith("EXISTING CARDS:\n- Where is ATP made? -> mitochondria")
        assert reconcile.coverage_messages(self._unit(), [])[1]["content"].endswith("EXISTING CARDS:\n(no cards)")

    def test_review_sees_the_candidates_next_to_the_deck(self):
        candidates = [{"type": "basic", "front": "Which organelle makes ATP?", "back": "The mitochondria"}]
        system, user = [m["content"] for m in reconcile.additions_messages(self._unit(), ["Where is ATP made? -> mitochondria"], candidates)]
        assert "when in doubt, reject it" in system and "Rejecting every candidate is a fine answer" in system
        assert "EXISTING CARDS:\n- Where is ATP made? -> mitochondria\n\nCANDIDATES:\n[0] Which organelle makes ATP? -> The mitochondria" in user

    def test_review_returns_a_decision_per_candidate_it_ruled_on(self):
        answer = json.dumps({"decisions": [
            {"index": 0, "add": False, "reason": " Repeats 'Where is ATP made?' "}, {"index": 1, "add": True, "reason": "new"},
            {"index": 7, "add": True, "reason": "not a candidate"},
        ]})
        client = FakeClient([fake_response(answer)])
        cards = [{"type": "basic", "front": f"q{i}", "back": "a"} for i in range(3)]
        out = reconcile.review_additions(client, self._unit(), [], cards)
        assert out["decisions"] == {0: {"add": False, "reason": "Repeats 'Where is ATP made?'"}, 1: {"add": True, "reason": "new"}}


# ------------------------------------------------------------ figure analysis
class TestFigureAnalysis:
    def test_vision_is_asked_what_a_figure_adds(self):
        from app.services.pipeline import figures

        required = figures.VISION_SCHEMA["json_schema"]["schema"]["required"]
        assert {"text_only", "transcript", "adds", "suggested_cards"} <= set(required)
        system = figures.VISION_SYSTEM
        assert "Exercise and quiz questions" in system and "something to do, not something to learn" in system
        assert "Most figures are worth 0 to 2" in system and "incidental details of a single example" in system
        assert "does not show" in system

    def test_advice_is_bounded_and_a_text_image_is_a_transcript(self):
        from app.services.pipeline import figures

        assert figures.advised_cards({"suggested_cards": 40}) == 8 and figures.advised_cards({"suggested_cards": "x"}) == 0
        assert figures.advised_cards(None) == 0 and figures.advised_cards({"suggested_cards": 3}) == 3
        table = {"text_only": True, "transcript": " A ∩ B = B ∩ A \r\n\n\n\n\nA ∪ B = B ∪ A "}
        assert figures.transcript_of(table) == "A ∩ B = B ∩ A\n\n\nA ∪ B = B ∪ A"
        # Only a figure the vision pass called text is taken at its transcript.
        assert figures.transcript_of({"text_only": False, "transcript": "labels"}) == ""
        assert figures.transcript_of({"text_only": True, "transcript": "  "}) == "" and figures.transcript_of(None) == ""
        assert figures.transcript_block(11, "Set laws. ", "A ∩ B = B ∩ A") == "Text from an image on p.11 (Set laws):\nA ∩ B = B ∩ A"
        described = figures.describe_figure({"caption": "c", "adds": "The layout."})
        assert described.endswith("What it adds beyond the text: The layout.")


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


# ---------------------------------------------------------------- cheat sheet
class TestCheatSheet:
    def _units(self):
        units = _units(3, skipped=(2,))
        units[1].title, units[1].kind, units[1].text = "Enzyme kinetics", "formulas", "v = Vmax[S] / (Km + [S])"
        return units

    def test_prompt_is_framed_as_an_exam_cheat_sheet(self):
        units = self._units()
        system = cheatsheet.build_messages(units[1], units, {"focus": "kinetics"})[0]["content"]
        assert "bring a cheat sheet into the exam room" in system
        assert "lose marks" in system
        # Bare bones: every concept and its edge cases, nothing the source does not give.
        assert "complete in breadth and minimal in depth" in system
        assert "edge cases" in system and "difficulty for its own sake" in system
        assert "only an example the source itself gives" in system
        assert "Never make one up" in system
        # The sheet's page typesets maths written the way the cards write it.
        assert r"Math: \( ... \) inline and \[ ... \] for a formula on a line of its own. Never $...$." in system
        # Deck settings stay out of the system prompt so it caches across decks.
        assert system == cheatsheet.CHEATSHEET_SYSTEM

    def test_user_message_carries_the_brief_and_the_section(self):
        units = self._units()
        settings = {"exam_context": "Biochem midterm", "focus": "kinetics", "exclude": "history", "glossary": "Km"}
        user = cheatsheet.build_messages(units[1], units, settings, {"subject": "Biochemistry"})[1]["content"]
        for expected in ("Biochem midterm", "kinetics", "history", "Km", "Biochemistry"):
            assert expected in user
        assert "- Enzyme kinetics  <- this section" in user
        assert "- U0\n" in user and "U2" not in user  # skipped units are left off the outline
        assert user.endswith("SECTION: Enzyme kinetics (kind: formulas)\n\nv = Vmax[S] / (Km + [S])")

    def _figures(self):
        return [
            {"number": 2, "page": 7, "kind": "chart", "caption": "Rate against  substrate concentration",
             "description": "A hyperbolic curve.", "parts": ["x-axis: [S]"], "facts": ["Rate plateaus at Vmax"]},
            {"number": 5, "page": 8, "kind": "chart", "caption": "Lineweaver-Burk plot", "description": "",
             "parts": [], "facts": []},
        ]

    def test_section_diagrams_are_listed_for_the_writer(self):
        units = self._units()
        user = cheatsheet.build_messages(units[1], units, {}, figures=self._figures())[1]["content"]
        listing, section = user.split("SECTION: ")
        assert "[[Figure 2]] (p.7, chart) Rate against  substrate concentration" in listing
        assert "  Shows: A hyperbolic curve.\n  Labelled: x-axis: [S]\n  Conveys: Rate plateaus at Vmax" in listing
        assert "[[Figure 5]] (p.8, chart) Lineweaver-Burk plot\n\n" in listing
        assert section.endswith("v = Vmax[S] / (Km + [S])")
        assert "Diagrams" not in cheatsheet.build_messages(units[1], units, {}, figures=[])[1]["content"]

    def test_every_diagram_ends_up_on_the_sheet_exactly_once(self):
        figures = self._figures()
        written = "\n".join([
            "## Kinetics",
            "- Km is the [S] at half of Vmax (see [[Figure 2]]).",
            "- [[figure 2]]: the curve",
            "[[Figure 2]]",  # a repeat
            "[[Figure 9]]",  # not one of this section's diagrams
            "- Vmax is the highest rate.",
        ])
        assert cheatsheet.place_figures(written, figures) == "\n".join([
            "## Kinetics",
            "- Km is the [S] at half of Vmax (see Figure 2).",
            "[[Figure 2]] Rate against substrate concentration",
            "- Vmax is the highest rate.",
            "",
            "[[Figure 5]] Lineweaver-Burk plot",  # left out by the writer, kept anyway
        ])
        # A section the writer emptied still carries its diagrams.
        assert cheatsheet.place_figures("", figures[1:]) == "[[Figure 5]] Lineweaver-Burk plot"
        assert cheatsheet.place_figures("- Km is ...", []) == "- Km is ..."

    def test_a_diagram_without_cards_comes_off_the_sheet(self):
        sheet = "## Kinetics\n- Km is the [S] at half of Vmax (see Figure 2).\n[[Figure 2]] Rate against [S]\n\n[[Figure 5]] Lineweaver-Burk plot"
        assert cheatsheet.remove_figures(sheet, {5}) == (
            "## Kinetics\n- Km is the [S] at half of Vmax (see Figure 2).\n[[Figure 2]] Rate against [S]")
        assert cheatsheet.remove_figures(sheet, {2, 5}) == "## Kinetics\n- Km is the [S] at half of Vmax (see Figure 2)."
        assert cheatsheet.remove_figures("[[Figure 5]] Lineweaver-Burk plot", {5}) == ""
        assert cheatsheet.remove_figures(sheet, set()) == sheet

    def test_sheet_is_parsed_into_blocks_for_the_page(self):
        sheet = "\n".join([
            "## Kinetics",
            "- Km is the [S] at half of Vmax.",
            "  - Example: Vmax 10, rate 5 at [S] = 2, so Km = 2.",
            "[[Figure 2]] Rate against substrate concentration",
            "1. Measure the rate.",
            "   Repeat at each [S].",
            "",
            "| Inhibitor | Km |",
            "|---|---|",
            "| Competitive | rises |",
            "",
            "**Limits**",
            "Holds only at steady state,",
            "with [S] far above [E].",
        ])
        blocks = cheatsheet.sheet_blocks(sheet)
        assert [b["type"] for b in blocks] == ["heading", "list", "figure", "list", "table", "heading", "text"]
        assert blocks[0]["text"] == "Kinetics" and blocks[5]["text"] == "Limits"
        first, example = blocks[1]["items"]
        assert not first["sub"] and not first["example"]
        assert example["sub"] and example["example"]
        assert blocks[2]["number"] == 2
        assert blocks[3]["items"] == [
            {"text": "Measure the rate. Repeat at each [S].", "label": "1.", "sub": False, "example": False}]
        assert blocks[4]["rows"] == [["Inhibitor", "Km"], ["Competitive", "rises"]]
        assert blocks[6]["text"] == "Holds only at steady state, with [S] far above [E]."
        assert cheatsheet.sheet_blocks("") == []

    def test_a_bar_inside_maths_or_code_stays_in_its_table_cell(self):
        sheet = "\n".join([
            "| Pattern | Meaning |",
            "|---|---|",
            r"| \(|x| \le 1\) | `a|b` is `a` or `b` |",
            r"| $|\vec{v}|$ | \[|A| = ad - bc\] |",
            "| costs $5 | $6 | **a | b** |",
        ])
        assert cheatsheet.sheet_blocks(sheet)[0]["rows"] == [
            ["Pattern", "Meaning"],
            [r"\(|x| \le 1\)", "`a|b` is `a` or `b`"],
            [r"$|\vec{v}|$", r"\[|A| = ad - bc\]"],
            ["costs $5", "$6", "**a", "b**"],
        ]

    def test_write_returns_the_cleaned_sheet(self):
        raw = "\n## Kinetics  \n- Km is ...\r\n\n\n\n\n- Vmax is ...\n"
        client = FakeClient([fake_response(json.dumps({"cheat_sheet": raw}))])
        out = cheatsheet.write_cheat_sheet(client, [{"role": "user", "content": "x"}])
        assert out["cheat_sheet"] == "## Kinetics\n- Km is ...\n\n\n- Vmax is ..."
        assert out["usage"]["prompt_tokens"] == 10 and out["model"] == "m"

    def test_truncated_sheet_is_rejected(self):
        class Truncating(FakeClient):
            def chat(self, role, messages, **kwargs):
                result = super().chat(role, messages, **kwargs)
                result.finish_reason = "length"
                return result

        client = Truncating([fake_response(json.dumps({"cheat_sheet": "- half a"}))])
        with pytest.raises(Exception, match="cut off"):
            cheatsheet.write_cheat_sheet(client, [{"role": "user", "content": "x"}])

    def test_planner_is_told_only_when_the_sheet_is_on(self):
        units = _units(2)
        told = planner._planner_user_message(units, {"cheat_sheet": True}, 10, {})
        assert "exam cheat sheet" in told and "[[Figure N]]" in told
        assert "cheat sheet" not in planner._planner_user_message(units, {}, 10, {})
        assert "cheat sheet" not in planner._planner_user_message(units, {"cheat_sheet": False}, 10, {})

    def test_phase_is_listed_only_for_decks_that_asked(self):
        assert "cheatsheet" not in dict(phases_for({}))
        assert "cheatsheet" not in dict(phases_for(None))
        # Figures are read before the plan either way: the planner decides them, and the
        # sheet keeps the diagrams.
        assert [k for k, _ in phases_for({})][:4] == ["map", "figures", "plan", "write"]
        keys = [k for k, _ in phases_for({"cheat_sheet": True})]
        assert keys[:5] == ["map", "figures", "cheatsheet", "plan", "write"]
        assert keys == [k for k, _ in PHASES]
        # Duplicates are resolved after the coverage back-fill, so its cards are de-duplicated too.
        assert keys.index("coverage") < keys.index("reconcile") < keys.index("finish")


def test_critic_reads_the_figure_as_part_of_the_source():
    source = critic.figure_source("- The chloroplast is the site of photosynthesis.", "Caption: Chloroplast")
    user = critic.judge_messages([{"type": "basic", "front": "q", "back": "a"}], {}, source)[1]["content"]
    before_cards = user.split("\n\nCARDS:")[0]
    assert before_cards.startswith("SOURCE:\n- The chloroplast is the site of photosynthesis.")
    assert "FIGURE (part of the source" in before_cards and before_cards.endswith("Caption: Chloroplast")
