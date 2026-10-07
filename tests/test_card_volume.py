"""How many cards a run makes, and why: figures are decided by the planner, cards are
filed under the unit they came from, and a back-fill card has to earn its place."""

import json

import pytest

from app.extensions import db as _db
from app.models import Card, Deck, Figure, PipelineTask, Source
from app.services import deckgen
from app.services.llm import OpenRouterError
from app.services.pipeline import orchestrator
from app.services.pipeline.document_map import Unit

from conftest import fake_response, login, tool_call
from test_deckgen import FigureLLM, _figure_cards, _generate, _install_fake, _make_deck, _make_figure_deck


def _script_planner(fake, *decisions, group=False):
    """One text task per unit (or one over all of them with `group`), then `decisions`
    as further tool calls: (tool name, arguments)."""

    def planner(messages):
        fake.planner_turn += 1
        if fake.planner_turn > 1:
            return fake_response(None, tool_calls=[tool_call("f", "finish_plan", {"summary": "Scripted."})])
        idxs = [int(line.split("]")[0][1:]) for line in messages[1]["content"].splitlines() if line.startswith("[")]
        calls = [
            tool_call(f"s{n}", "spawn_task", {"unit_idxs": unit_idxs, "strategy": "general", "target_cards": 3, "notes": ""})
            for n, unit_idxs in enumerate([idxs] if group else [[i] for i in idxs])
        ]
        calls += [tool_call(f"d{n}", name, args) for n, (name, args) in enumerate(decisions)]
        return fake_response(None, tool_calls=calls)

    fake._planner = planner


def _card(front, back, quote=None):
    return {"type": "basic", "front": front, "back": back, "cloze_text": None, "extra": None, "tags": [], "source_quote": quote}


def _planner_brief(fake):
    return next(c for c in fake.calls if c["tools"])["messages"][1]["content"]


def _writes(fake):
    return [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "anki_cards"]


def _phase_result(deck_id, phase):
    return PipelineTask.query.filter_by(deck_id=deck_id, kind="phase", phase=phase).one().result_json


# ------------------------------------------------------------ figures in the plan
@pytest.mark.parametrize("cheat_sheet", [False, True])
def test_the_planner_decides_which_figures_get_cards(app, monkeypatch, cheat_sheet):
    deck_id, fake, _vision = _make_figure_deck(app, monkeypatch, {"cheat_sheet": cheat_sheet})
    _script_planner(fake, ("skip_figure", {"figure": 1, "reason": "The text states it."}))
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        # The planner was shown the diagram, and not the photo the vision pass set aside.
        brief = _planner_brief(fake)
        assert "F1: unit 0 · p.1 · diagram · vision suggests 2 card(s)" in brief and "A leaf" not in brief
        diagram = Figure.query.filter_by(deck_id=deck_id, hash="diagram").one()
        assert deck.run_json["plan"]["figure_skips"] == {str(diagram.id): "The text states it."}
        assert not _figure_cards(deck_id)
        assert not [w for w in _writes(fake) if "FIGURE under study" in w]
        assert deck.run_json["stats"]["by_origin"]["figures"] == 0
        if cheat_sheet:
            # The sheet was written with the diagram on it; without cards it comes off again,
            # so the sheet and the image cards stay the same set.
            sheet_call = next(c for c in fake.calls if c["name"] == "cheat_sheet")["messages"][1]["content"]
            assert "[[Figure 1]] (p.1, diagram) Chloroplast" in sheet_call
            unit = Source.query.filter_by(deck_id=deck_id, idx=0).one()
            assert unit.text == "- Photosynthesis converts CO2 and water into glucose using light."
            assert deck.run_json["cheat_sheet"]["figures"] == 0 and "taken off the sheet" in brief


def test_a_figure_is_written_after_its_units_text_and_shown_those_cards(app, monkeypatch):
    deck_id, fake, _vision = _make_figure_deck(app, monkeypatch, {})
    _script_planner(fake, ("spawn_figure_task", {"figure": 1, "target_cards": 1, "notes": "Only the stroma."}))
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        diagram = Figure.query.filter_by(deck_id=deck_id, hash="diagram").one()
        planned = [t for t in deck.run_json["plan"]["tasks"] if t["figure_id"]]
        assert [(t["figure_id"], t["strategy"], t["target_cards"], t["notes"], t["unit_idxs"]) for t in planned] == [
            (diagram.id, "figure_recall", 1, "Only the stroma.", [0])]
        # Counted in the plan: the estimate the planner started from includes the figure.
        assert deck.run_json["budget"] == deck.run_json["plan"]["budget"] and "figures included" in _planner_brief(fake)

        writes = _writes(fake)
        figure_write, = [w for w in writes if "FIGURE under study" in w]
        text_writes = [w for w in writes if "FIGURE under study" not in w and "Coverage gap-fill" not in w]
        assert all(writes.index(w) < writes.index(figure_write) for w in text_writes)
        assert "The deck already has these cards" in figure_write
        assert "- What does photosynthesis produce? -> Glucose" in figure_write
        assert "Planner notes: Only the stroma." in figure_write
        assert not [w for w in text_writes if "The deck already has" in w]
        assert all("an estimate, not a quota" in w for w in writes)

        node = PipelineTask.query.filter_by(deck_id=deck_id, phase="write", strategy="figure_recall").one()
        assert node.label == "Figure on p.1: Chloroplast"
        assert [c.figure_id for c in _figure_cards(deck_id)] == [diagram.id]
        assert deck.run_json["stats"]["by_origin"]["figures"] == 1


@pytest.mark.parametrize("cheat_sheet", [False, True])
def test_a_figure_that_is_only_text_joins_its_units_text(app, monkeypatch, cheat_sheet):
    deck_id, fake, _vision = _make_figure_deck(app, monkeypatch, {"cheat_sheet": cheat_sheet})
    transcript = "Photosystem II splits water.\nPhotosystem I makes NADPH."

    def analyze(client, image, mime, context):
        table = image == b"diagram"
        return {"useful": table, "kind": "table" if table else "decorative", "caption": "Light reactions" if table else "A leaf",
                "description": "", "parts": [], "facts": [], "text_only": table, "transcript": transcript if table else "",
                "adds": "", "suggested_cards": 3 if table else 0}

    monkeypatch.setattr(orchestrator.figures_mod, "analyze_figure", analyze)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        assert _phase_result(deck_id, "figures")["transcribed"] == 1 and _phase_result(deck_id, "figures")["pictures"] == 0
        # It is text now: nothing for the planner to rule on, no image on a card or the sheet.
        assert "Figures (" not in _planner_brief(fake)
        plan = deck.run_json["plan"]
        assert not [t for t in plan["tasks"] if t["figure_id"]] and plan["figure_skips"] == {}
        assert not _figure_cards(deck_id)
        block = f"Text from an image on p.1 (Light reactions):\n{transcript}"
        unit = Source.query.filter_by(deck_id=deck_id, idx=0).one()
        if cheat_sheet:
            section = next(c for c in fake.calls if c["name"] == "cheat_sheet")["messages"][1]["content"]
            assert section.endswith(block) and "Diagrams in this section" not in section
            assert "[[Figure" not in unit.text and deck.run_json["cheat_sheet"]["figures"] == 0
        else:
            assert unit.text.endswith(block)
            assert any(block in w for w in _writes(fake))


def test_figures_in_a_skipped_unit_are_not_read(app, monkeypatch):
    read = []

    def analyze(client, image, mime, context):
        read.append(image)
        return {"useful": True, "kind": "diagram", "caption": "Cell", "description": "", "parts": [], "facts": [],
                "text_only": False, "transcript": "", "adds": "", "suggested_cards": 2}

    monkeypatch.setattr(orchestrator.figures_mod, "analyze_figure", analyze)
    deck_id = _make_deck(app, settings={"use_figures": True})
    with app.app_context():
        _db.session.add_all([Figure(deck_id=deck_id, page=1, hash="a", image=b"kept"),
                             Figure(deck_id=deck_id, page=2, hash="b", image=b"exercise")])
        _db.session.commit()
        ctx = orchestrator._build_context(_db.session.get(Deck, deck_id))
        ctx.units = [Unit(idx=0, title="Cells", text="Cells.", char_start=0, char_end=6, page_start=1, page_end=1),
                     Unit(idx=1, title="Exercises", text="Q1.", char_start=6, char_end=9, page_start=2, page_end=2,
                          skipped=True, skip_reason="practice questions")]
        ctx.unit_by_idx = {u.idx: u for u in ctx.units}
        orchestrator._phase_figures(ctx)
        assert read == [b"kept"]
        result = _phase_result(deck_id, "figures")
        assert (result["figures"], result["read"], result["pictures"]) == (2, 1, 1)
        assert [f["unit_idx"] for f in orchestrator._plan_figures(ctx)] == [0]


def test_a_figure_on_a_page_without_text_continues_the_unit_before_it(app):
    deck_id = _make_deck(app)
    with app.app_context():
        ctx = orchestrator._build_context(_db.session.get(Deck, deck_id))
        ctx.units = [Unit(idx=0, title="Sets", text="x", char_start=0, char_end=1, page_start=2, page_end=3),
                     Unit(idx=1, title="Automata", text="y", char_start=1, char_end=2, page_start=3, page_end=6),
                     Unit(idx=2, title="Grammars", text="z", char_start=2, char_end=3, page_start=12, page_end=14)]
        ctx.unit_by_idx = {u.idx: u for u in ctx.units}
        where = lambda page: orchestrator._unit_idx_for_page(ctx, page)  # noqa: E731
        assert [where(2), where(5), where(13)] == [0, 1, 2]
        # Slides 7 to 11 are pictures only: they belong to the topic they follow, not the next one.
        assert [where(7), where(11), where(40)] == [1, 1, 2]
        assert where(1) == 0 and where(None) is None


@pytest.mark.parametrize("figures_read", [True, False])
def test_a_plan_paused_before_figures_were_planned_still_gets_them(app, monkeypatch, figures_read):
    deck_id, _fake, vision = _make_figure_deck(app, monkeypatch, {"review_plan": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "planned"
        # What an older version stored: text tasks only, and no word on the figures.
        run = dict(deck.run_json)
        plan = dict(run["plan"])
        assert plan.pop("figure_skips") == {} and any(t["figure_id"] for t in plan["tasks"])
        plan["tasks"] = [t for t in plan["tasks"] if not t["figure_id"]]
        run["plan"] = plan
        deck.run_json = run
        if not figures_read:
            PipelineTask.query.filter_by(deck_id=deck_id, phase="figures").delete()
        _db.session.commit()
        assert _generate(app, monkeypatch, deck_id, resume_from_plan=True) == deck_id
        assert _db.session.get(Deck, deck_id).status == "ready"
        assert [c.status for c in _figure_cards(deck_id)] == ["ok"]
        assert len(vision) == (2 if figures_read else 4)


def test_regenerating_a_unit_keeps_its_figure_cards(app, monkeypatch):
    deck_id, _fake, _vision = _make_figure_deck(app, monkeypatch, {})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        unit = Source.query.filter_by(deck_id=deck_id, idx=0).one()
        figure_card, = _figure_cards(deck_id)
        assert figure_card.source_id == unit.id
        before = {c.id for c in Card.query.filter_by(source_id=unit.id).all()}
        assert deckgen.regenerate_source(unit.id) == unit.id
        after = {c.id for c in Card.query.filter_by(source_id=unit.id).all()}
        assert before & after == {figure_card.id} and len(after) > 1


def test_plan_review_shows_figure_tasks_and_what_was_left_out(app, client, monkeypatch):
    from app.routes import main

    deck_id, _fake, _vision = _make_figure_deck(app, monkeypatch, {"review_plan": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        diagram_id = Figure.query.filter_by(deck_id=deck_id, hash="diagram").one().id
        tasks = _db.session.get(Deck, deck_id).run_json["plan"]["tasks"]
        figure_task, = [t for t in tasks if t["figure_id"]]
        text_task = next(t for t in tasks if not t["figure_id"])
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/plan").text
    assert "<b>Figure 1 · Chloroplast</b>" in page and f'<img src="/figures/{diagram_id}.png" alt="Chloroplast"' in page
    assert f'name="target_{figure_task["id"]}"' in page and f'name="skip_{figure_task["id"]}"' in page
    # A figure task has one way of writing; no task is offered the figure strategy.
    assert f'name="strategy_{figure_task["id"]}"' not in page and 'value="figure_recall"' not in page
    assert "given no cards" not in page and ">estimate " in page

    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    client.post(f"/decks/{deck_id}/plan", data={
        "action": "run", f"target_{figure_task['id']}": "1", f"notes_{figure_task['id']}": "Only the stroma.",
        f"strategy_{figure_task['id']}": "general", f"strategy_{text_task['id']}": "figure_recall",
    })
    with app.app_context():
        by_id = {t["id"]: t for t in _db.session.get(Deck, deck_id).run_json["plan"]["tasks"]}
        kept = by_id[figure_task["id"]]
        assert (kept["strategy"], kept["figure_id"], kept["target_cards"], kept["notes"]) == (
            "figure_recall", diagram_id, 1, "Only the stroma.")
        assert by_id[text_task["id"]]["strategy"] == text_task["strategy"] != "figure_recall"


def test_plan_review_lists_the_figures_given_no_cards(app, client, monkeypatch):
    deck_id, fake, _vision = _make_figure_deck(app, monkeypatch, {"review_plan": True, "target_cards": 30})
    _script_planner(fake, ("skip_figure", {"figure": 1, "reason": "The text states it."}))
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/plan").text
    assert "1 figure given no cards" in page and "<b>Figure 1</b> Chloroplast" in page and "The text states it." in page
    assert "pt-figure" not in page and ">target 30<" in page


# -------------------------------------------------- cards filed under their unit
def test_cards_of_a_multi_unit_task_are_filed_under_their_own_unit(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    _script_planner(fake, group=True)

    def write(messages):
        if "Coverage gap-fill" in messages[-1]["content"]:
            return fake_response(json.dumps({"cards": []}))
        return fake_response(json.dumps({"cards": [
            _card("What does photosynthesis produce?", "Glucose", "into glucose"),
            _card("Where is the energy from respiration stored?", "ATP", "energy  stored\nas ATP"),
            _card("What consumes oxygen?", "Respiration", "a quote that is nowhere in the source"),
        ]}))

    fake._anki_cards = write
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert [t["unit_idxs"] for t in deck.run_json["plan"]["tasks"]] == [[0, 1]]
        idx_of = {s.id: s.idx for s in Source.query.filter_by(deck_id=deck_id).all()}
        filed = {c.front: (idx_of[c.source_id], [t for t in c.tags if t.startswith("unit:")]) for c in Card.query.filter_by(deck_id=deck_id)}
        # By the unit its quote is from; a card whose quote cannot be placed goes to the first.
        assert filed == {
            "What does photosynthesis produce?": (0, ["unit:1"]),
            "Where is the energy from respiration stored?": (1, ["unit:2"]),
            "What consumes oxygen?": (0, ["unit:1"]),
        }
        # The audit of the second unit sees that unit's cards, so it is not back-filled as if it had none.
        audits = {c["messages"][-1]["content"].split("\n")[0]: c["messages"][-1]["content"]
                  for c in fake.calls if c["name"] == "coverage_audit"}
        assert set(audits) == {"UNIT: Unit 0", "UNIT: Unit 1"}
        assert "- Where is the energy from respiration stored? -> ATP" in audits["UNIT: Unit 1"]
        # A card of the task that could not be placed still counts for every unit the task read.
        assert "- What consumes oxygen? -> Respiration" in audits["UNIT: Unit 1"]
        assert not [a for a in audits.values() if a.endswith("(no cards)")]
        assert not [c for c in fake.calls if c["name"] == "backfill_review"]
        assert _phase_result(deck_id, "coverage")["candidates"] == 0


# --------------------------------------------------------------- the back-fill
def test_duplicates_are_resolved_after_the_backfill(app, client, monkeypatch):
    _install_fake(monkeypatch)
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        phases = [t.phase for t in PipelineTask.query.filter_by(deck_id=deck_id, kind="phase").order_by(PipelineTask.seq)]
        assert phases == ["map", "figures", "plan", "write", "critique", "coverage", "reconcile", "finish"]
        # Each unit's audit asked for the same gap card; both passed the review, and the
        # de-duplication that now follows kept one.
        gap_cards = [c for c in Card.query.filter_by(deck_id=deck_id).all() if "origin:coverage" in c.tags]
        assert sorted(c.status for c in gap_cards) == ["deleted", "ok"]
        assert any("dedupe:exact" in c.tags for c in gap_cards if c.status == "deleted")
        coverage = _phase_result(deck_id, "coverage")
        assert (coverage["candidates"], coverage["rejected"], coverage["cards_added"]) == (2, 0, 2)

        stats = deck.run_json["stats"]
        ok = Card.query.filter_by(deck_id=deck_id, status="ok").count()
        assert stats["by_origin"] == {"text": ok - 1, "figures": 0, "backfill": 1}
        assert stats["budget"] == deck.run_json["budget"] > 0 and stats["backfill_rejected"] == 0
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/status").text
    assert f"Estimated {stats['budget']} · kept {ok}: {ok - 1} from the text, 1 back-filled" in page


def test_a_backfill_card_that_repeats_the_deck_is_turned_away(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    first_pass = fake._anki_cards

    def write(messages):
        user = messages[-1]["content"]
        if "Coverage gap-fill" not in user:
            return first_pass(messages)
        # The gap writer is shown what the unit already has, and repeats one of them anyway.
        assert "The deck already has these cards" in user and "- What does photosynthesis produce? -> Glucose" in user
        return fake_response(json.dumps({"cards": [_card("What does photosynthesis produce?", "Glucose", "into glucose")]}))

    fake._anki_cards = write
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        reviews = [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "backfill_review"]
        assert len(reviews) == 2 and all("\n\nCANDIDATES:\n[0] What does photosynthesis produce? -> Glucose" in r for r in reviews)
        turned_away = [c for c in Card.query.filter_by(deck_id=deck_id).all() if "origin:coverage" in c.tags]
        assert len(turned_away) == 2
        for card in turned_away:
            # Kept as deleted, with the reason, so the student can read it and restore it.
            assert card.status == "deleted" and "backfill:rejected" in card.tags
            assert card.critic_json["verdict"] == "drop"
            assert card.critic_json["reason"] == "Back-fill review: repeats an existing card"
        coverage = _phase_result(deck_id, "coverage")
        assert (coverage["candidates"], coverage["rejected"], coverage["cards_added"]) == (2, 2, 0)
        nodes = [t for t in PipelineTask.query.filter_by(deck_id=deck_id, phase="coverage", kind="task").all()
                 if t.label.startswith("Review 1 back-fill card ·")]
        assert len(nodes) == 2 and all((n.cards_made, n.cards_kept) == (1, 0) for n in nodes)
        stats = deck.run_json["stats"]
        assert stats["by_origin"]["backfill"] == 0 and stats["backfill_rejected"] == 2


def test_a_backfill_that_could_not_be_reviewed_waits_for_the_student(app, monkeypatch):
    fake = _install_fake(monkeypatch)

    def down(messages):
        raise OpenRouterError("upstream down", status_code=503)

    fake._backfill_review = down
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        gap_cards = [c for c in Card.query.filter_by(deck_id=deck_id).all() if "origin:coverage" in c.tags]
        # Unreviewed is not approved: they are not exported until the student passes them.
        assert [c.status for c in gap_cards] == ["needs_review", "needs_review"]
        assert all("backfill:unreviewed" in c.tags for c in gap_cards)
        assert deck.run_json["stats"]["by_origin"]["backfill"] == 0


@pytest.mark.parametrize("target_cards", [None, 30])
def test_a_deck_the_student_sized_is_backfilled_only_for_the_likely_questions(app, monkeypatch, target_cards):
    fake = _install_fake(monkeypatch)
    fake._coverage_audit = lambda messages: fake_response(json.dumps({"coverage_score": 80, "missing": [
        {"fact": "Oxygen is released.", "importance": 2, "source_quote": "releasing oxygen"},
    ]}))
    deck_id = _make_deck(app, settings={"target_cards": target_cards})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        gap_cards = [c for c in Card.query.filter_by(deck_id=deck_id).all() if "origin:coverage" in c.tags]
        brief = _planner_brief(fake)
        if target_cards:
            assert not gap_cards and _phase_result(deck_id, "coverage")["gap_tasks"] == 0
            assert "The student asked for about 30 cards in total" in brief
        else:
            assert gap_cards and "not a limit" in brief


class WorthlessFigureCards(FigureLLM):
    """A judge that finds the figure's card not worth learning."""

    def _critic_verdicts(self, messages):
        response = super()._critic_verdicts(messages)
        verdicts = json.loads(response["choices"][0]["message"]["content"])["verdicts"]
        if "stroma" in messages[-1]["content"].split("\nCARDS:\n", 1)[-1]:
            for verdict in verdicts:
                verdict.update(worthwhile=False, verdict="drop", reason="a detail of this one picture")
        return fake_response(json.dumps({"verdicts": verdicts}))


def test_the_critic_drops_a_card_that_is_not_worth_learning(app, monkeypatch):
    deck_id, _fake, _vision = _make_figure_deck(app, monkeypatch, {})
    from app.services import llm as llm_module

    fake = WorthlessFigureCards()
    monkeypatch.setattr(llm_module, "openrouter_chat", fake)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        card, = _figure_cards(deck_id)
        assert card.status == "deleted" and "critic:not_worthwhile" in card.tags
        assert card.critic_json["worthwhile"] is False and card.critic_json["reason"] == "a detail of this one picture"
        # The judge was told what makes a figure card not worth keeping.
        judged = next(c for c in fake.calls if c["name"] == "critic_verdicts" and "stroma" in c["messages"][-1]["content"])
        assert "incidental detail of this one picture" in judged["messages"][-1]["content"]
