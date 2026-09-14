"""Capture real app pages with synthetic data in a disposable local database.

Optional tooling: pip install playwright && python -m playwright install chromium
Run from the repository root: python -m scripts.capture_docs
"""
import hashlib
import secrets
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Card, Deck, Source, User
from app.services.pipeline.planner import Plan, PlanTask


def main():
    output = Path(__file__).resolve().parents[1] / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ankigpt-docs-") as temporary:
        class DemoConfig(Config):
            SECRET_KEY = secrets.token_hex(32)
            SQLALCHEMY_DATABASE_URI = "sqlite:///" + (Path(temporary) / "demo.db").as_posix()
            UPLOAD_FOLDER = temporary
            OPENROUTER_API_KEY = ""
            SESSION_COOKIE_SECURE = False

        app = create_app(DemoConfig)
        password = secrets.token_urlsafe(24)
        with app.app_context():
            user = User(email="learner@example.test", display_name="Alex Morgan", avatar_color="sage")
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            sections = [
                ("Active recall", "definitions", "Active recall means retrieving information from memory without looking at the answer.", "definition_sweep", "Define active recall and distinguish it from rereading."),
                ("Spaced practice", "procedure", "Spaced practice distributes study sessions over time instead of concentrating them into one session.", "mechanism_chain", "Connect spacing, retrieval, and long-term retention."),
                ("Interleaving", "comparison", "Interleaving mixes different problem types during practice so learners must choose the appropriate method.", "compare_contrast", "Contrast mixed practice with blocked practice."),
            ]
            source = "# Learning that lasts\n\n" + "\n\n".join(f"## {s[0]}\n{s[2]}" for s in sections)
            deck = Deck(user_id=user.id, title="Learning that lasts", card_style="mixed", status="ready", source_type="text", source_text=source)
            db.session.add(deck)
            db.session.flush()
            tasks = []
            for idx, (title, kind, text, strategy, notes) in enumerate(sections, 1):
                unit = Source(deck_id=deck.id, idx=idx, title=title, kind=kind, text=text, hash=hashlib.sha256(text.encode()).hexdigest(), density=3, summary=notes)
                db.session.add(unit)
                db.session.flush()
                tasks.append(PlanTask(idx, [idx], strategy, 4, notes))
                card = Card(deck_id=deck.id, source_id=unit.id, type="cloze" if idx == 2 else "basic", status="ok", strategy=strategy, difficulty=1 if idx == 1 else 2, source_quote=text, tags=["learning", title.lower().replace(" ", "_")], order_key=idx)
                if idx == 1:
                    card.front = "What makes a study activity active recall?"
                    card.back = "Retrieving information from memory without looking at the answer."
                elif idx == 2:
                    card.cloze_text = "Spaced practice distributes study sessions {{c1::over time}}."
                    card.extra = "Contrast this with concentrating study into one session."
                else:
                    card.front = "What does interleaving require the learner to choose?"
                    card.back = "The appropriate method for each problem type."
                db.session.add(card)
            deck.run_json = {"plan": Plan(tasks=tasks, budget=12, turns=4, summary="Build the core definitions first, then connect study methods and contrast when to use them.").to_dict()}
            db.session.commit()
            deck_id = deck.id
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1040}, device_scale_factor=1.5, reduced_motion="reduce")
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/auth/login")
                page.locator('input[name="email"]').fill("learner@example.test")
                page.locator('input[name="password"]').fill(password)
                page.locator('button[type="submit"]').click()
                page.wait_for_url("**/decks")
                for name, route in [("editor", f"/decks/{deck_id}"), ("new-deck", "/decks/new"), ("plan", f"/decks/{deck_id}/plan")]:
                    if name == "plan":
                        with app.app_context():
                            db.session.get(Deck, deck_id).status = "planned"
                            db.session.commit()
                    response = page.goto(base + route)
                    assert response.status == 200
                    page.evaluate("document.fonts.ready")
                    page.wait_for_timeout(800)
                    if name == "editor":
                        page.set_viewport_size({"width": 1440, "height": 820})
                    page.screenshot(path=str(output / f"{name}.png"), full_page=name != "editor")
                    page.set_viewport_size({"width": 1440, "height": 1040})
                browser.close()
        finally:
            server.shutdown()
            thread.join()
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
    print(f"Saved screenshots to {output}")


if __name__ == "__main__":
    main()
