"""High-yield cheat sheet: only what the exam asks the student to do goes on the sheet,
each line tagged with it, ranked against the whole document, one card per core line, and
what was left off can be brought back at plan review."""

import json

import pytest

from app.extensions import db as _db
from app.models import Card, Deck, PipelineTask, Source
from app.services.pipeline import cheatsheet
from app.services.pipeline.document_map import Unit
from app.services.pipeline.workers import HIGH_YIELD_BRIEF

from conftest import fake_response, login
from test_deckgen import _generate, _install_fake, _make_deck

# Three sentences in the first section, so the fake writer gives it a core line, a
# context point (left off) and a supporting line; two in the second.
SOURCE = """# Photosynthesis

Photosynthesis converts CO2 and water into glucose using light. In 1779 Jan Ingenhousz
showed that plants need light to make oxygen. Chlorophyll is the green pigment that absorbs
the light.

# Respiration

Cellular respiration oxidises glucose to release energy stored as ATP. A marathon runner
burns about 2,600 kcal in a race."""

CORE_0 = "Photosynthesis converts CO2 and water into glucose using light."
CONTEXT_0 = "In 1779 Jan Ingenhousz showed that plants need light to make oxygen."
SUPPORTING_0 = "Chlorophyll is the green pigment that absorbs the light."
CORE_1 = "Cellular respiration oxidises glucose to release energy stored as ATP."
CONTEXT_1 = "A marathon runner burns about 2,600 kcal in a race."

HIGH_YIELD = {"cheat_sheet": True, "high_yield": True}


def _unit(text, idx=0, density=3):
    return Unit(idx=idx, title=f"Unit {idx}", text=text, char_start=0, char_end=len(text), density=density)


# ------------------------------------------------------------------ the sheet itself
class TestSheet:
    def test_the_writer_s_points_become_tagged_lines_and_what_it_left_off(self):
        sheet, left_off = cheatsheet.render_high_yield({"sections": [
            {"heading": "Ideal gas", "items": [
                {"text": r"Use \(PV = nRT\) to find moles from P, V and T.", "tier": "core", "asks": "calculate"},
                {"text": "R is 8.314 J/(mol K).", "tier": "supporting", "asks": "recall"},
                {"text": "Boyle first studied gases in 1662.", "tier": "context", "asks": "recall"},
                {"text": "[[Figure 2]]", "tier": "core", "asks": "recall"},
                {"text": "  ", "tier": "core", "asks": "define"},
            ]},
            {"heading": "Worked example", "items": [
                {"text": "The 1986 Challenger seals failed in the cold.", "tier": "context", "asks": "nonsense"},
            ]},
            {"heading": "", "items": [{"text": "Set up a gas-law problem by\n listing knowns first.", "tier": "core", "asks": "apply"}]},
        ]})
        assert sheet == "\n".join([
            "### Ideal gas",
            r"- [calculate] Use \(PV = nRT\) to find moles from P, V and T.",
            "- [supporting] R is 8.314 J/(mol K).",
            "[[Figure 2]]",
            "",
            "- [apply] Set up a gas-law problem by listing knowns first.",
        ])
        assert left_off == [
            {"heading": "Ideal gas", "text": "Boyle first studied gases in 1662.", "asks": "recall"},
            {"heading": "Worked example", "text": "The 1986 Challenger seals failed in the cold.", "asks": "recall"},
        ]
        assert cheatsheet.render_high_yield({"sections": []}) == ("", [])

    def test_a_section_counts_its_core_and_supporting_lines(self):
        sheet = "\n".join([
            "### Gases",
            "- [calculate] Use PV = nRT.",
            "- [supporting] R is 8.314.",
            "- [supporting] T is in kelvin.",
            "- [supporting] P is in pascals.",
            "[[Figure 1]] A piston",
            "### Rules",
            "- [Distinguish] Isothermal keeps T fixed; adiabatic allows no heat flow.",
        ])
        assert cheatsheet.yield_counts(sheet) == (2, 3)
        assert cheatsheet.yield_target(sheet) == 3  # used only for a deck the student sized
        assert cheatsheet.yield_target("- A plain cheat-sheet line.") is None

    def test_the_page_shows_each_line_s_tag_as_a_label(self):
        items = cheatsheet.sheet_blocks("### Gases\n- [calculate] Use PV = nRT.\n- [supporting] R is 8.314.\n- Plain line.")[1]["items"]
        assert [(i["tag"], i["text"]) for i in items] == [
            ("calculate", "Use PV = nRT."), ("supporting", "R is 8.314."), (None, "Plain line.")]

    def test_lines_are_resorted_and_points_brought_back(self):
        sheet = "### Gases\n- [calculate] Use PV = nRT.\n- [supporting] R is 8.314.\n[[Figure 1]] A piston\n\n### Units\n- [define] A mole is 6.022e23 particles."
        left = [
            {"heading": "Gases", "text": "Boyle studied gases in 1662.", "asks": "recall"},
            {"heading": "History", "text": "Avogadro proposed his law in 1811.", "asks": "recall"},
            {"heading": "Gases", "text": "Gases expand to fill their container.", "asks": "define"},
        ]
        rows = cheatsheet.sheet_lines(sheet)
        assert [(n, tag) for n, tag, _text in rows] == [(1, "calculate"), (2, "supporting"), (6, "define")]
        text, left_after = cheatsheet.retier(sheet, left, {2: "explain", 6: "off"}, {0: "recall", 1: "supporting"})
        assert text == "\n".join([
            "### Gases",
            "- [calculate] Use PV = nRT.",
            "- [explain] R is 8.314.",
            "[[Figure 1]] A piston",
            "- [recall] Boyle studied gases in 1662.",
            "",
            "### History",
            "- [supporting] Avogadro proposed his law in 1811.",
        ])
        # The line taken off joins what was left off, under its heading; the one not touched stays.
        assert left_after == [
            {"heading": "Gases", "text": "Gases expand to fill their container.", "asks": "define"},
            {"heading": "Units", "text": "A mole is 6.022e23 particles.", "asks": "define"},
        ]
        assert cheatsheet.retier(sheet, left, {}, {}) == (sheet, left)
        assert cheatsheet.retier("### Units\n- [define] A mole.", [], {1: "off"}, {})[0] == ""

    def test_high_yield_needs_a_cheat_sheet(self):
        assert cheatsheet.high_yield(HIGH_YIELD)
        assert not cheatsheet.high_yield({"high_yield": True})
        assert not cheatsheet.high_yield({"cheat_sheet": True})

    def test_the_prompts_are_written_for_any_subject(self):
        for prompt in (cheatsheet.HIGH_YIELD_SYSTEM, cheatsheet.HIGH_YIELD_RANK_SYSTEM):
            assert "programming language" in prompt and "spoken language" in prompt
            assert "case stud" not in prompt.lower() and "econom" not in prompt.lower()
        # A fact is core only when the fact itself is what gets tested.
        assert "A statistic, a percentage, a date or a list of places" in cheatsheet.HIGH_YIELD_SYSTEM
        assert "is several points" in cheatsheet.HIGH_YIELD_SYSTEM


# ------------------------------------------------------------------ the ranking
class TestRanking:
    def _units(self):
        return [
            _unit("### A\n- [explain] Central idea.\n- [recall] A statistic.\n- [define] A term.\n- [supporting] Background.", 0),
            _unit("### B\n- [recall] Same central idea again.", 1),
            _unit("Kept in full: no tags.", 2),
        ]

    def test_every_core_point_of_the_document_is_rated_together(self):
        units = self._units()
        points = cheatsheet.core_points(units)
        assert [(pid, unit.idx, n, tag) for pid, unit, n, tag, _t in points] == [
            (0, 0, 1, "explain"), (1, 0, 2, "recall"), (2, 0, 3, "define"), (3, 1, 1, "recall")]
        user = cheatsheet.build_rank_messages(points, units, {"exam_context": "Spanish 2 oral exam"}, {"subject": "Biology"})[1]["content"]
        assert "[0] (Unit 0) explain: Central idea." in user and "[3] (Unit 1) recall: Same central idea again." in user
        assert "Spanish 2 oral exam" in user and "- Unit 2" in user and "part 1 of" not in user
        part = cheatsheet.build_rank_messages(points[:2], units, {}, part=(1, 2))[1]["content"]
        assert "These are part 1 of 2" in part

    def test_a_rating_below_four_loses_its_card(self):
        points = cheatsheet.core_points(self._units())
        choices = cheatsheet.ranked_choices(points, {"0": 5, "1": 2, "2": 3, "3": 1})
        assert choices == {0: {2: "off", 3: "supporting"}, 1: {1: "off"}}
        # A point the model gave no score keeps the writer's tier.
        assert cheatsheet.ranked_choices(points, {"0": 5}) == {}


def _ranking(scores):
    """A ranking call that gives each listed point the score `scores` has for its text."""
    def rank(messages):
        out = []
        for line in messages[-1]["content"].splitlines():
            if line.startswith("["):
                pid, text = int(line.split("]")[0][1:]), line.split(": ", 1)[1]
                out.append({"id": pid, "score": scores.get(text, 5)})
        return fake_response(json.dumps({"scores": out}))
    return rank


# ------------------------------------------------------------------- whole runs
def _sheet_calls(fake):
    return [c for c in fake.calls if c["name"] in ("cheat_sheet", "high_yield_sheet")]


def test_a_high_yield_run_writes_only_what_the_exam_asks(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app, source_text=SOURCE, settings={**HIGH_YIELD, "exam_context": "Calculations final"})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"

        # The high-yield writer, briefed with the student's exam, wrote every section.
        sheets = _sheet_calls(fake)
        assert [c["name"] for c in sheets] == ["high_yield_sheet", "high_yield_sheet"]
        assert "WHAT AN EXAM ACTUALLY ASKS" in sheets[0]["messages"][0]["content"]
        assert "Calculations final" in sheets[0]["messages"][1]["content"]

        # Then one call rated the document's core points together.
        rank, = [c for c in fake.calls if c["name"] == "high_yield_ranking"]
        assert f"[0] (Unit 0) explain: {CORE_0}" in rank["messages"][1]["content"]
        assert f"[1] (Unit 1) explain: {CORE_1}" in rank["messages"][1]["content"]
        assert SUPPORTING_0 not in rank["messages"][1]["content"]

        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert units[0].text == f"### Key points\n- [explain] {CORE_0}\n- [supporting] {SUPPORTING_0}"
        assert units[1].text == f"### Key points\n- [explain] {CORE_1}"
        stats = deck.run_json["cheat_sheet"]
        assert (stats["high_yield"], stats["core"], stats["supporting"], stats["left_off_count"]) == (True, 2, 1, 2)
        assert stats["ranked"] == {"core_before": 2, "scored": 2}
        assert stats["left_off"] == {
            "0": [{"heading": "Key points", "text": CONTEXT_0, "asks": "recall"}],
            "1": [{"heading": "Key points", "text": CONTEXT_1, "asks": "recall"}],
        }

        # The planner is told how the sheet works, and no card count at all.
        brief = next(c for c in fake.calls if c["tools"])["messages"][1]["content"]
        assert "high-yield exam sheet" in brief and "One task per unit" in brief
        assert "suggested cards" not in brief and "target_cards" not in brief and "cards in total" not in brief

        # Workers shape their cards by the tags; nothing downstream saw what was left off.
        writes = [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "anki_cards"]
        assert writes and all(HIGH_YIELD_BRIEF in w and "write about" not in w for w in writes)
        later = [c for c in fake.calls if c["tools"] or c["name"] in ("anki_cards", "critic_verdicts")]
        assert all(CONTEXT_0 not in str(c["messages"]) and CONTEXT_1 not in str(c["messages"]) for c in later)

        # No back-fill: a high-yield deck is meant to leave things out.
        assert not [c for c in fake.calls if c["name"] in ("coverage_audit", "backfill_review")]
        coverage = PipelineTask.query.filter_by(deck_id=deck_id, kind="phase", phase="coverage").one()
        assert coverage.status == "skipped" and coverage.result_json == {"reason": "high-yield deck"}
        assert not [c for c in Card.query.filter_by(deck_id=deck_id).all() if "origin:coverage" in c.tags]
        assert Card.query.filter_by(deck_id=deck_id, status="ok").count() >= 1


def test_the_ranking_demotes_what_the_whole_course_does_not_ask(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    fake._high_yield_ranking = _ranking({CORE_1: 2})
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        # Unit 1's only core line was rated 2: it comes off, and the section with it.
        assert units[1].skipped and units[1].skip_reason == cheatsheet.EMPTY_REASON
        stats = _db.session.get(Deck, deck_id).run_json["cheat_sheet"]
        assert stats["left_off"]["1"] == [
            {"heading": "Key points", "text": CONTEXT_1, "asks": "recall"},
            {"heading": "Key points", "text": CORE_1, "asks": "explain"},
        ]
        assert (stats["core"], stats["ranked"]["core_before"]) == (1, 2)
        writes = [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "anki_cards"]
        assert writes and all(CORE_1 not in w for w in writes)
        node = PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet", kind="task").order_by(
            PipelineTask.seq.desc()).first()
        assert node.label == "Rank 2 core points across the document · 1 kept"


def test_a_section_left_with_only_supporting_lines_comes_off(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    fake._high_yield_ranking = _ranking({CORE_0: 3})
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        unit = Source.query.filter_by(deck_id=deck_id, idx=0).one()
        # Its core line became supporting, so nothing in it gets a card: all of it is left
        # off, where the plan review can still bring a line back.
        assert unit.skipped
        left = _db.session.get(Deck, deck_id).run_json["cheat_sheet"]["left_off"]["0"]
        assert [p["text"] for p in left] == [CONTEXT_0, CORE_0, SUPPORTING_0]


def test_a_long_document_is_ranked_in_parts(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    monkeypatch.setattr(cheatsheet, "RANK_BATCH", 1)
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        ranks = [c["messages"][1]["content"] for c in fake.calls if c["name"] == "high_yield_ranking"]
        assert len(ranks) == 2 and "part 1 of 2" in ranks[0] and "part 2 of 2" in ranks[1]


def test_a_failed_ranking_leaves_the_sheet_as_written(app, monkeypatch):
    fake = _install_fake(monkeypatch)

    def broken(messages):
        return fake_response("not json at all")

    fake._high_yield_ranking = broken
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        assert _db.session.get(Deck, deck_id).status == "ready"
        assert Source.query.filter_by(deck_id=deck_id, idx=1).one().text == f"### Key points\n- [explain] {CORE_1}"
        node = PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet", kind="task").order_by(
            PipelineTask.seq.desc()).first()
        assert node.status == "failed" and node.label.startswith("Rank 2 core points")


def test_a_section_with_nothing_the_exam_asks_is_left_off_whole(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    original = fake._high_yield_sheet

    def writer(messages):
        if "SECTION: Unit 1" in messages[-1]["content"]:
            return fake_response(
                '{"sections": [{"heading": "Case", "items": [{"text": "A runner burns 2,600 kcal.", "tier": "context", "asks": "recall"}]}]}')
        return original(messages)

    fake._high_yield_sheet = writer
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        unit = Source.query.filter_by(deck_id=deck_id, idx=1).one()
        assert unit.skipped and unit.skip_reason == cheatsheet.EMPTY_REASON
        stats = _db.session.get(Deck, deck_id).run_json["cheat_sheet"]
        assert stats["emptied"] == 1 and stats["left_off"]["1"][0]["text"] == "A runner burns 2,600 kcal."


def test_high_yield_is_only_saved_with_a_cheat_sheet(app, client, monkeypatch):
    from app.routes import main

    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    app.config["OPENROUTER_API_KEY"] = "test-key"
    deck_id = _make_deck(app, source_text=SOURCE)
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/preview").text
    assert 'name="high_yield"' in page and "data-high-yield hidden" in page

    client.post(f"/decks/{deck_id}/preview", data={"high_yield": "on", "target_cards": "auto"})
    with app.app_context():
        assert _db.session.get(Deck, deck_id).settings_json["high_yield"] is False
    client.post(f"/decks/{deck_id}/preview", data={"cheat_sheet": "on", "high_yield": "on", "target_cards": "auto"})
    with app.app_context():
        settings = _db.session.get(Deck, deck_id).settings_json
        assert settings["cheat_sheet"] is True and settings["high_yield"] is True
    assert "data-high-yield hidden" not in client.get(f"/decks/{deck_id}/preview").text


def _planned_high_yield_deck(app, monkeypatch, **settings):
    _install_fake(monkeypatch)
    deck_id = _make_deck(app, source_text=SOURCE, settings={**HIGH_YIELD, "review_plan": True, **settings})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "planned"
        tasks = {t["unit_idxs"][0]: t for t in deck.run_json["plan"]["tasks"]}
    return deck_id, tasks


def test_plan_review_lists_the_sheet_and_brings_lines_back(app, client, monkeypatch):
    from app.routes import main

    deck_id, tasks = _planned_high_yield_deck(app, monkeypatch)
    # On auto no task has a count.
    assert {t["target_cards"] for t in tasks.values()} == {0}
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/plan").text
    assert "High-yield sheet" in page and "<b>2</b> core · 1 supporting · 2 left off" in page
    assert 'name="tier_0_1"' in page and 'name="tier_0_2"' in page and 'name="left_0_0"' in page
    assert '<option value="explain" selected>Card · explain</option>' in page and CONTEXT_0 in page
    assert f'name="target_{tasks[0]["id"]}" value="" placeholder="auto"' in page
    assert "each writer decides how many cards its material earns" in page

    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    response = client.post(f"/decks/{deck_id}/plan", data={
        "action": "run",
        f"target_{tasks[0]['id']}": "",
        # Unit 0: the left-off point gets a card, the supporting line is promoted.
        "left_0_0": "recall", "tier_0_2": "define",
        # Unit 1: its only line is left off, so the unit has nothing to write from.
        "tier_1_1": "off",
    })
    assert response.status_code == 302
    with app.app_context():
        deck = _db.session.get(Deck, deck_id)
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert units[0].text == "\n".join([
            "### Key points", f"- [explain] {CORE_0}", f"- [define] {SUPPORTING_0}", f"- [recall] {CONTEXT_0}"])
        assert units[1].skipped and units[1].skip_reason == main.REVIEW_EMPTIED_REASON
        plan = deck.run_json["plan"]
        by_unit = {t["unit_idxs"][0]: t for t in plan["tasks"]}
        # Still no count: the writer writes a card per core line. Unit 1's task is gone.
        assert by_unit[0]["target_cards"] == 0 and 1 not in by_unit
        assert plan["skips"]["1"] == main.REVIEW_EMPTIED_REASON
        stats = deck.run_json["cheat_sheet"]
        assert (stats["core"], stats["supporting"], stats["left_off_count"]) == (3, 0, 2)
        assert stats["left_off"]["1"] == [
            {"heading": "Key points", "text": CONTEXT_1, "asks": "recall"},
            {"heading": "Key points", "text": CORE_1, "asks": "explain"},
        ]


def test_plan_review_moves_a_count_the_student_sized_with_the_sheet(app, client, monkeypatch):
    from app.routes import main

    deck_id, tasks = _planned_high_yield_deck(app, monkeypatch, target_cards=30)
    assert tasks[0]["target_cards"] > 0
    login(client, email="gen@example.com")
    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    client.post(f"/decks/{deck_id}/plan", data={
        "action": "run", f"target_{tasks[0]['id']}": str(tasks[0]["target_cards"]), "left_0_0": "recall", "tier_0_2": "define",
    })
    with app.app_context():
        plan = _db.session.get(Deck, deck_id).run_json["plan"]
        # Two more core lines, two more cards.
        assert {t["unit_idxs"][0]: t["target_cards"] for t in plan["tasks"]}[0] == tasks[0]["target_cards"] + 2


def test_plan_review_keeps_a_card_count_the_student_set(app, client, monkeypatch):
    from app.routes import main

    deck_id, tasks = _planned_high_yield_deck(app, monkeypatch)
    login(client, email="gen@example.com")
    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    client.post(f"/decks/{deck_id}/plan", data={"action": "run", f"target_{tasks[0]['id']}": "7", "left_0_0": "recall"})
    with app.app_context():
        plan = _db.session.get(Deck, deck_id).run_json["plan"]
        assert {t["unit_idxs"][0]: t["target_cards"] for t in plan["tasks"]}[0] == 7


def test_plan_review_gives_a_left_off_section_a_task_when_a_line_comes_back(app, client, monkeypatch):
    from app.routes import main

    deck_id, tasks = _planned_high_yield_deck(app, monkeypatch)
    login(client, email="gen@example.com")
    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    # First leave unit 1 off entirely; then, on a fresh pause, bring a point back.
    with app.app_context():
        unit = Source.query.filter_by(deck_id=deck_id, idx=1).one()
        unit.skipped, unit.skip_reason = True, cheatsheet.EMPTY_REASON
        deck = _db.session.get(Deck, deck_id)
        run = dict(deck.run_json)
        run["plan"] = {**run["plan"], "tasks": [t for t in run["plan"]["tasks"] if t["unit_idxs"] != [1]],
                       "skips": {"1": cheatsheet.EMPTY_REASON}}
        deck.run_json = run
        _db.session.commit()
    assert "left off entirely" in client.get(f"/decks/{deck_id}/plan").text
    client.post(f"/decks/{deck_id}/plan", data={"action": "run", "left_1_0": "calculate"})
    with app.app_context():
        unit = Source.query.filter_by(deck_id=deck_id, idx=1).one()
        assert not unit.skipped and unit.text == f"### Key points\n- [calculate] {CONTEXT_1}"
        plan = _db.session.get(Deck, deck_id).run_json["plan"]
        task, = [t for t in plan["tasks"] if t["unit_idxs"] == [1]]
        assert (task["origin"], task["target_cards"]) == ("review", 0) and "1" not in plan["skips"]


def test_cheat_sheet_page_shows_the_tags_and_what_was_left_off(app, client, monkeypatch):
    _install_fake(monkeypatch)
    deck_id = _make_deck(app, source_text=SOURCE, settings=HIGH_YIELD)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/cheat-sheet").text
    assert f'<span class="tag">explain</span> {CORE_0}' in page
    assert f'tagged supporting"><span class="tag">supporting</span> {SUPPORTING_0}' in page
    assert "1 point left off" in page and CONTEXT_0 in page
    assert "2 core" in page and "Only what the exam asks you to do" in page


@pytest.mark.parametrize("high_yield", [False, True])
def test_retry_keeps_the_high_yield_setting(app, client, monkeypatch, high_yield):
    with app.app_context():
        deck_id = _make_deck(app, source_text=SOURCE, settings={"cheat_sheet": True, "high_yield": high_yield})
        deck = _db.session.get(Deck, deck_id)
        deck.status, deck.run_json = "failed", {"last_error": "boom"}
        _db.session.commit()
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/status").text
    assert ('name="high_yield" value="on"' in page) is high_yield
