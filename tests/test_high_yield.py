"""High-yield cheat sheet: only what the exam asks the student to do goes on the sheet,
each line tagged with it, one card per core line, and what was left off can be brought
back at plan review."""

import pytest

from app.extensions import db as _db
from app.models import Card, Deck, Source
from app.services.pipeline import cheatsheet, planner
from app.services.pipeline.document_map import Unit
from app.services.pipeline.planner import Plan, PlanTask
from app.services.pipeline.workers import HIGH_YIELD_BRIEF

from conftest import login
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
            {"heading": "Case study", "items": [
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
            {"heading": "Case study", "text": "The 1986 Challenger seals failed in the cold.", "asks": "recall"},
        ]
        assert cheatsheet.render_high_yield({"sections": []}) == ("", [])

    def test_a_section_is_worth_a_card_per_core_line(self):
        sheet = "\n".join([
            "### Gases",
            "- [calculate] Use PV = nRT.",
            "- [supporting] R is 8.314.",
            "- [supporting] T is in kelvin.",
            "- [supporting] P is in pascals.",
            "### Units",
            "- [supporting] 1 atm is 101,325 Pa.",
            "[[Figure 1]] A piston",
            "### Rules",
            "- [Distinguish] Isothermal keeps T fixed; adiabatic allows no heat flow.",
        ])
        assert cheatsheet.yield_counts(sheet) == (2, 4)
        assert cheatsheet.yield_target(sheet) == 3  # two core lines, plus one for four supporting lines
        assert cheatsheet.yield_target("- A plain cheat-sheet line.") is None
        # The audit only checks the core lines; a heading with none under it goes too.
        assert cheatsheet.core_text(sheet) == "\n".join([
            "### Gases", "- [calculate] Use PV = nRT.", "### Rules",
            "- [Distinguish] Isothermal keeps T fixed; adiabatic allows no heat flow.",
        ])
        assert cheatsheet.core_text("### Only\n- [supporting] background") == ""

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


# ---------------------------------------------------------------------- sizing
class TestSizing:
    def test_a_high_yield_unit_is_sized_by_its_core_lines_not_its_length(self):
        long_line = "x" * 3000
        tagged = _unit(f"- [explain] {long_line}\n- [recall] Short.\n- [supporting] Background.", density=5)
        assert planner.heuristic_target(tagged) == 2
        assert planner.heuristic_target(tagged, "definition_sweep") == 2
        # The same length untagged is sized from its characters, as before.
        assert planner.heuristic_target(_unit(f"- {long_line}", density=5)) > 10

    def test_a_high_yield_plan_cannot_run_far_past_its_estimate(self):
        def plan(settings):
            units = [_unit("- [recall] A fact.", idx=i) for i in range(4)]
            p = Plan(budget=100, tasks=[PlanTask(id=i + 1, unit_idxs=[i], strategy="general", target_cards=75) for i in range(4)])
            return sum(t.target_cards for t in planner._finalize(units, p, factor=planner.ceiling_factor(settings)).tasks)

        assert plan(HIGH_YIELD) == 128  # 1.3x the estimate is 130, and each of 4 tasks rounds to 32
        assert plan({"cheat_sheet": True}) == 248  # 2.5x: 250, each task 62
        # A size the student typed is theirs, so the usual runaway guard applies.
        assert planner.ceiling_factor({**HIGH_YIELD, "target_cards": 50}) == planner.CEILING_FACTOR


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

        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert units[0].text == f"### Key points\n- [explain] {CORE_0}\n- [supporting] {SUPPORTING_0}"
        assert units[1].text == f"### Key points\n- [explain] {CORE_1}"
        stats = deck.run_json["cheat_sheet"]
        assert (stats["high_yield"], stats["core"], stats["supporting"], stats["left_off_count"]) == (True, 2, 1, 2)
        assert stats["left_off"] == {
            "0": [{"heading": "Key points", "text": CONTEXT_0, "asks": "recall"}],
            "1": [{"heading": "Key points", "text": CONTEXT_1, "asks": "recall"}],
        }

        # The planner was told to plan a card per core line and shown that count per unit.
        brief = next(c for c in fake.calls if c["tools"])["messages"][1]["content"]
        assert "high-yield exam sheet" in brief and "One task per unit" in brief
        assert brief.count("suggested cards: 1") == 2 and "stay within about 15%" in brief

        # Workers shape their cards by the tags; nothing downstream saw what was left off.
        writes = [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "anki_cards"]
        assert writes and all(HIGH_YIELD_BRIEF in w for w in writes)
        later = [c for c in fake.calls if c["tools"] or c["name"] in ("anki_cards", "critic_verdicts", "coverage_audit")]
        assert all(CONTEXT_0 not in str(c["messages"]) and CONTEXT_1 not in str(c["messages"]) for c in later)

        # The coverage audit only checks the core lines.
        audits = [c["messages"][-1]["content"] for c in fake.calls if c["name"] == "coverage_audit"]
        assert len(audits) == 2 and CORE_0 in audits[0] and SUPPORTING_0 not in audits[0]
        assert Card.query.filter_by(deck_id=deck_id, status="ok").count() >= 1


def test_a_section_with_nothing_the_exam_asks_is_left_off_whole(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    original = fake._high_yield_sheet

    def writer(messages):
        if "SECTION: Unit 1" in messages[-1]["content"]:
            return __import__("conftest").fake_response(
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


def _planned_high_yield_deck(app, monkeypatch):
    _install_fake(monkeypatch)
    deck_id = _make_deck(app, source_text=SOURCE, settings={**HIGH_YIELD, "review_plan": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "planned"
        tasks = {t["unit_idxs"][0]: t for t in deck.run_json["plan"]["tasks"]}
    return deck_id, tasks


def test_plan_review_lists_the_sheet_and_brings_lines_back(app, client, monkeypatch):
    from app.routes import main

    deck_id, tasks = _planned_high_yield_deck(app, monkeypatch)
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/plan").text
    assert "High-yield sheet" in page and "<b>2</b> core · 1 supporting · 2 left off" in page
    assert 'name="tier_0_1"' in page and 'name="tier_0_2"' in page and 'name="left_0_0"' in page
    assert '<option value="explain" selected>Card · explain</option>' in page and CONTEXT_0 in page

    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    response = client.post(f"/decks/{deck_id}/plan", data={
        "action": "run",
        f"target_{tasks[0]['id']}": str(tasks[0]["target_cards"]),
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
        # Unit 0 went from one card's worth to three; unit 1's task is gone with it.
        assert by_unit[0]["target_cards"] == tasks[0]["target_cards"] + 2 and 1 not in by_unit
        assert plan["skips"]["1"] == main.REVIEW_EMPTIED_REASON
        stats = deck.run_json["cheat_sheet"]
        assert (stats["core"], stats["supporting"], stats["left_off_count"]) == (3, 0, 2)
        assert stats["left_off"]["1"] == [
            {"heading": "Key points", "text": CONTEXT_1, "asks": "recall"},
            {"heading": "Key points", "text": CORE_1, "asks": "explain"},
        ]


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
        assert (task["origin"], task["target_cards"]) == ("review", 1) and "1" not in plan["skips"]


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
