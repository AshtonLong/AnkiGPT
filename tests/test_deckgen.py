"""End-to-end generation through the agentic pipeline with a scripted model."""

import json

from app.extensions import db as _db
from app.models import Card, Deck, LLMRun, PipelineTask, Source, User
from app.services import deckgen
from app.services import llm as llm_module
from app.services.llm import OpenRouterError

from conftest import FakeLLM, fake_embeddings, fake_response

SOURCE = """# Photosynthesis

Photosynthesis converts CO2 and water into glucose using light. Water is split during
the light reactions, releasing oxygen.

# Respiration

Cellular respiration oxidises glucose to release energy stored as ATP. It happens in the
mitochondria of eukaryotic cells and consumes oxygen."""


def _make_deck(app, source_text=SOURCE, settings=None, card_style="mixed"):
    with app.app_context():
        user = User.query.filter_by(email="gen@example.com").first()
        if not user:
            user = User(email="gen@example.com")
            user.set_password("password123")
            _db.session.add(user)
            _db.session.commit()
        deck = Deck(
            user_id=user.id,
            title="Gen",
            card_style=card_style,
            status="draft",
            source_type="text",
            source_text=source_text,
            settings_json=settings or {},
            run_json={},
        )
        _db.session.add(deck)
        _db.session.commit()
        return deck.id


def _install_fake(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_module, "openrouter_chat", fake)
    monkeypatch.setattr(llm_module, "openrouter_embeddings", fake_embeddings)
    return fake


def test_generate_deck_end_to_end(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        # Force the mapper to run (the source is short) so the whole path is exercised.
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        assert deckgen.generate_deck(deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"

        # Document map: two heading-delimited units, second depends on first.
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert len(units) == 2
        assert units[1].depends_on == [0]

        # Planner ran as an agent: it read a unit, spawned tasks, and finished.
        run = deck.run_json
        assert run["plan"]["mode"] == "agent"
        assert len(run["plan"]["tasks"]) == 2
        assert run["plan"]["summary"] == "One task per unit."
        strategies = {t["strategy"] for t in run["plan"]["tasks"]}
        assert strategies == {"definition_sweep", "general"}

        # Critic dropped the unsupported Krebs card, reconcile removed the cross-unit duplicate.
        cards = Card.query.filter_by(deck_id=deck_id).all()
        ok = [c for c in cards if c.status == "ok"]
        deleted = [c for c in cards if c.status == "deleted"]
        assert ok, "no surviving cards"
        assert any("critic:unsupported" in (c.tags or []) for c in deleted)
        assert any("dedupe:" in t for c in deleted for t in (c.tags or []))
        assert all("Krebs" not in (c.front or "") for c in ok)
        # Coverage audit spawned a gap-fill task whose card survived.
        assert any("origin:coverage" in (c.tags or []) for c in ok)
        # Every ok card carries provenance.
        assert all(c.strategy and c.source_id and c.task_id for c in ok)
        assert all(c.order_key for c in ok)

        # Trace tree: every phase present, worker tasks logged with LLM runs.
        phases = {t.phase for t in PipelineTask.query.filter_by(deck_id=deck_id, kind="phase").all()}
        assert {"map", "plan", "write", "critique", "reconcile", "coverage", "finish"} <= phases
        assert LLMRun.query.filter_by(deck_id=deck_id, role="worker").count() >= 2
        assert run["stats"]["cards_ok"] == len(ok)
        assert run["totals"]["calls"] > 0

        # Model routing: every call went to the configured default model.
        assert {c["model"] for c in fake.calls} == {app.config["OPENROUTER_MODEL"]}


def test_review_plan_pauses_then_resumes(app, monkeypatch):
    _install_fake(monkeypatch)
    deck_id = _make_deck(app, settings={"review_plan": True})
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        assert deckgen.generate_deck(deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "planned"
        assert Card.query.filter_by(deck_id=deck_id).count() == 0
        assert Source.query.filter_by(deck_id=deck_id).count() == 2
        # Resume from the stored plan: cards get written without re-planning.
        assert deckgen.generate_deck(deck_id, resume_from_plan=True) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        assert Card.query.filter_by(deck_id=deck_id, status="ok").count() >= 1


def test_failed_generation_is_non_destructive(app, monkeypatch):
    """If mapping/planning fails, the deck is marked failed and pre-existing cards survive."""
    deck_id = _make_deck(app)
    with app.app_context():
        existing = Card(deck_id=deck_id, type="basic", front="old", back="card", status="ok")
        _db.session.add(existing)
        _db.session.commit()
        existing_id = existing.id

    def always_fail(*args, **kwargs):
        raise OpenRouterError("upstream down", status_code=503)

    monkeypatch.setattr(llm_module, "openrouter_chat", always_fail)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        # The mapper fails before anything is wiped.
        result = deckgen.generate_deck(deck_id)
        assert result is None
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert (deck.run_json or {}).get("last_error")
        # The earlier card must survive a failed regeneration.
        assert _db.session.get(Card, existing_id) is not None


def test_terminal_auth_error_fails_fast(app, monkeypatch):
    calls = []

    def auth_fail(*args, **kwargs):
        calls.append(1)
        raise OpenRouterError("bad key", status_code=401)

    monkeypatch.setattr(llm_module, "openrouter_chat", auth_fail)
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        assert deckgen.generate_deck(deck_id) is None
        assert _db.session.get(Deck, deck_id).status == "failed"
        assert "authentication" in _db.session.get(Deck, deck_id).run_json["last_error"].lower()
        # Aborted on the first terminal error instead of hammering the API per task.
        assert len(calls) <= 2


def test_missing_api_key_marks_failed(app, monkeypatch):
    # Reached when a user removes their key after a run was queued.
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = ""
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        assert deckgen.generate_deck(deck_id) is None
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert "Add yours under My profile" in deck.run_json["last_error"]


def test_improve_card_uses_structured_output(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = "test-key"
        card = Card(deck_id=deck_id, type="basic", front="q", back="a", status="ok")
        _db.session.add(card)
        _db.session.commit()
        assert deckgen.improve_card(card.id) == card.id
        _db.session.refresh(card)
        assert card.front == "Improved front?"
        assert fake.calls[-1]["name"] == "improved_basic_card"


# ------------------------------------------------------------- cheat sheet first
DROPPED_DETAIL = "releasing oxygen"  # second sentence of unit 0; the fake cheat sheet cuts it
DOWNSTREAM = {"anki_cards", "cold_answers", "critic_verdicts", "coverage_audit", "duplicate_resolution"}


def _generate(app, monkeypatch, deck_id, **kwargs):
    app.config["OPENROUTER_API_KEY"] = "test-key"
    monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
    return deckgen.generate_deck(deck_id, **kwargs)


def test_cheat_sheet_is_off_unless_asked_for(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        assert not [c for c in fake.calls if c["name"] == "cheat_sheet"]
        assert PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet").count() == 0
        assert not _db.session.get(Deck, deck_id).run_json.get("cheat_sheet")
        # Workers still read the source verbatim.
        assert DROPPED_DETAIL in Source.query.filter_by(deck_id=deck_id, idx=0).one().text


def test_cheat_sheet_becomes_the_source_for_every_later_phase(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app, settings={"cheat_sheet": True, "exam_context": "Bio 101 final", "glossary": "ATP"})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"

        # One cheat-sheet call per live unit, framed as an exam cheat sheet and briefed
        # with the student's settings.
        sheets = [c for c in fake.calls if c["name"] == "cheat_sheet"]
        assert len(sheets) == 2
        system, user = sheets[0]["messages"][0]["content"], sheets[0]["messages"][1]["content"]
        assert "bring one cheat sheet into the exam room" in system
        assert "Bio 101 final" in user and "ATP" in user

        # The units now hold the cheat sheet, sized as the densest kind of material.
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert units[0].text == "- Photosynthesis converts CO2 and water into glucose using light."
        assert units[1].text == "- Cellular respiration oxidises glucose to release energy stored as ATP."
        assert all(u.density == 5 and not u.skipped for u in units)

        # Planner, workers, critic and coverage never saw what the cheat sheet cut.
        later = [c for c in fake.calls if c["tools"] or c["name"] in DOWNSTREAM]
        assert {c["name"] for c in later} >= {"anki_cards", "critic_verdicts", "coverage_audit"}
        assert all(DROPPED_DETAIL not in str(c["messages"]) for c in later)
        planner_brief = next(c for c in fake.calls if c["tools"])["messages"][1]["content"]
        assert "exam cheat sheet" in planner_brief

        # Traced like any other phase: a node per unit, each call logged, totals on the run.
        phase = PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet", kind="phase").one()
        assert phase.status == "done"
        nodes = PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet", kind="task").all()
        assert len(nodes) == 2 and all(n.status == "done" and "→" in n.label for n in nodes)
        assert LLMRun.query.filter_by(deck_id=deck_id, role="cheatsheet").count() == 2
        stats = deck.run_json["cheat_sheet"]
        assert stats["condensed"] == 2 and 0 < stats["chars_out"] < stats["chars_in"]
        assert Card.query.filter_by(deck_id=deck_id, status="ok").count() >= 1


def test_cheat_sheet_survives_the_plan_review_pause(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app, settings={"cheat_sheet": True, "review_plan": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        assert _db.session.get(Deck, deck_id).status == "planned"
        assert DROPPED_DETAIL not in Source.query.filter_by(deck_id=deck_id, idx=0).one().text
        before = len(fake.calls)
        assert _generate(app, monkeypatch, deck_id, resume_from_plan=True) == deck_id
        assert _db.session.get(Deck, deck_id).status == "ready"
        resumed = fake.calls[before:]
        # Resuming writes from the stored cheat sheet; it is not rebuilt.
        assert resumed and not [c for c in resumed if c["name"] == "cheat_sheet"]
        assert all(DROPPED_DETAIL not in str(c["messages"]) for c in resumed)


def test_unit_keeps_full_text_when_its_cheat_sheet_fails(app, monkeypatch):
    fake = _install_fake(monkeypatch)

    def flaky(messages):
        if "SECTION: Unit 0" in messages[-1]["content"]:
            raise OpenRouterError("upstream down", status_code=503)
        return FakeLLM._cheat_sheet(fake, messages)

    fake._cheat_sheet = flaky
    deck_id = _make_deck(app, settings={"cheat_sheet": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        assert _db.session.get(Deck, deck_id).status == "ready"
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert DROPPED_DETAIL in units[0].text and not units[0].skipped
        assert units[1].text.startswith("- Cellular respiration")
        phase = PipelineTask.query.filter_by(deck_id=deck_id, phase="cheatsheet", kind="phase").one()
        assert phase.status == "failed" and "kept in full" in phase.error
        assert _db.session.get(Deck, deck_id).run_json["cheat_sheet"]["failed"] == 1


def test_unit_with_nothing_exam_critical_is_skipped(app, monkeypatch):
    fake = _install_fake(monkeypatch)

    def picky(messages):
        if "SECTION: Unit 1" in messages[-1]["content"]:
            return fake_response(json.dumps({"cheat_sheet": "  "}))
        return FakeLLM._cheat_sheet(fake, messages)

    fake._cheat_sheet = picky
    deck_id = _make_deck(app, settings={"cheat_sheet": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready"
        units = Source.query.filter_by(deck_id=deck_id).order_by(Source.idx).all()
        assert units[1].skipped and "cheat sheet" in units[1].skip_reason
        assert [t["unit_idxs"] for t in deck.run_json["plan"]["tasks"]] == [[0]]
        assert not Card.query.filter_by(deck_id=deck_id, source_id=units[1].id).count()


def test_empty_cheat_sheet_fails_without_wiping_the_deck(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    fake._cheat_sheet = lambda messages: fake_response(json.dumps({"cheat_sheet": ""}))
    deck_id = _make_deck(app, settings={"cheat_sheet": True})
    with app.app_context():
        existing = Card(deck_id=deck_id, type="basic", front="old", back="card", status="ok")
        _db.session.add(existing)
        _db.session.commit()
        existing_id = existing.id
        assert _generate(app, monkeypatch, deck_id) is None
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert "cheat sheet came back empty" in deck.run_json["last_error"]
        assert not [c for c in fake.calls if c["tools"]], "planner must not run on an empty cheat sheet"
        assert _db.session.get(Card, existing_id) is not None
