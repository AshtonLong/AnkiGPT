"""End-to-end generation through the agentic pipeline with a scripted model."""

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
    deck_id = _make_deck(app)
    with app.app_context():
        app.config["OPENROUTER_API_KEY"] = ""
        monkeypatch.setattr("app.services.pipeline.document_map.SINGLE_UNIT_MAX_CHARS", 10)
        assert deckgen.generate_deck(deck_id) is None
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert "OPENROUTER_API_KEY" in deck.run_json["last_error"]


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
