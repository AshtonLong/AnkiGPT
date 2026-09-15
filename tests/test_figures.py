"""Figure jobs must survive database commits while running outside Flask."""

import pytest
from flask import has_app_context

from app.extensions import db
from app.models import Deck, Figure, PipelineTask, User
from app.services.pipeline import orchestrator
from app.services.pipeline.document_map import Unit


@pytest.mark.parametrize("failures", [0, 1, 3])
def test_figure_jobs_use_snapshots_and_report_failures(app, monkeypatch, caplog, failures):
    received = []

    def analyze(client, image, mime, context):
        assert not has_app_context()
        received.append((image, mime, context))
        if int(image.decode()) < failures:
            raise RuntimeError("Vision service unavailable")
        return {"useful": True, "kind": "diagram", "caption": "Cell",
                "description": "A labelled cell", "parts": ["A: nucleus"],
                "facts": ["The nucleus contains DNA"], "suggested_cards": 2}

    monkeypatch.setattr(orchestrator.figures_mod, "analyze_figure", analyze)
    with app.app_context():
        user = User(email="figures@example.com", password_hash="unused")
        db.session.add(user)
        db.session.flush()
        deck = Deck(user_id=user.id, title="Figures", source_type="pdf", card_style="basic",
                    source_text="Cells contain nuclei.", settings_json={"use_figures": True})
        db.session.add(deck)
        db.session.flush()
        for i in range(3):
            db.session.add(Figure(deck_id=deck.id, page=1, hash=str(i),
                                  image=str(i).encode(), mime="image/png"))
        db.session.commit()
        ctx = orchestrator._build_context(deck)
        unit = Unit(idx=0, title="Cells", text=deck.source_text, char_start=0,
                    char_end=len(deck.source_text), page_start=1, page_end=1)
        ctx.units = [unit]
        ctx.unit_by_idx = {0: unit}

        # Uses the real thread pool and tracer (whose commits expire ORM objects).
        orchestrator._phase_figures(ctx)

        assert sorted(received) == [(str(i).encode(), "image/png", unit.text) for i in range(3)]
        assert len(ctx.figure_tasks) == 3 - failures
        assert all(t.figure_id and t.unit_idxs == [0] for t in ctx.figure_tasks)
        assert Figure.query.filter_by(deck_id=deck.id, useful=True).count() == 3 - failures
        phase = PipelineTask.query.filter_by(deck_id=deck.id, kind="phase", phase="figures").one()
        assert phase.status == ("failed" if failures else "done")
        assert phase.result_json["failed"] == failures
        assert phase.result_json["tasks"] == 3 - failures
        if failures:
            assert "Vision service unavailable" in caplog.text
            assert phase.error
