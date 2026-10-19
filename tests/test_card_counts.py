"""On auto, no card count reaches a model. A number in a model's context, even one called
an estimate, is a target it writes towards; so the planner and every writer are told what
to cover, never how many cards to make. A deck size the student sets still goes through."""

import json
import re

from app.extensions import db as _db
from app.models import Deck
from app.services.pipeline import planner, workers
from app.services.pipeline.document_map import Unit
from app.services.pipeline.planner import PlanTask

from conftest import fake_response, login, tool_call
from test_deckgen import _generate, _install_fake, _make_deck

NUMBER_OF_CARDS = re.compile(r"\b\d+\s+cards?\b|target_cards|suggested cards|budget", re.IGNORECASE)


def _units(n=2):
    return [Unit(idx=i, title=f"U{i}", text="Membranes are lipid bilayers. " * 40, char_start=0, char_end=0, density=4)
            for i in range(n)]


def _figure():
    return {"id": 11, "number": 1, "unit_idx": 0, "page": 3, "kind": "diagram", "caption": "Cell membrane",
            "adds": "Where each protein sits.", "suggested": 2}


def _model_text(messages, tools=None):
    return json.dumps([m.get("content") for m in messages]) + json.dumps(tools or [])


class TestThePlanner:
    def test_on_auto_its_prompt_and_tools_hold_no_count(self):
        units = _units()
        system = planner.planner_system(False)
        user = planner._planner_user_message(units, {}, 0, {}, [_figure()])
        tools = planner._tools(True, counted=False)
        assert not NUMBER_OF_CARDS.search(system + user + json.dumps(tools)), NUMBER_OF_CARDS.search(system + user + json.dumps(tools))
        assert "Do not think in card counts" in system and "vision: worth cards" in user
        spawn = next(t for t in tools if t["function"]["name"] == "spawn_task")["function"]["parameters"]
        assert spawn["required"] == ["unit_idxs", "strategy", "notes"]

    def test_a_deck_size_the_student_set_still_reaches_it(self):
        units = _units()
        settings = {"target_cards": 40}
        user = planner._planner_user_message(units, settings, 40, {}, [_figure()])
        assert "The student asked for about 40 cards" in user and "suggested cards:" in user
        spawn = next(t for t in planner._tools(True) if t["function"]["name"] == "spawn_task")["function"]["parameters"]
        assert spawn["required"] == ["unit_idxs", "strategy", "target_cards", "notes"]

    def test_on_auto_every_task_is_planned_without_one(self):
        from test_pipeline import FakeClient

        client = FakeClient([
            fake_response(None, tool_calls=[
                # A model may still send a count; it is not kept.
                tool_call("1", "spawn_task", {"unit_idxs": [0], "strategy": "general", "target_cards": 40, "notes": ""}),
                tool_call("2", "spawn_figure_task", {"figure": 1, "notes": "Labels only."}),
            ]),
            fake_response(None, tool_calls=[tool_call("3", "finish_plan", {"summary": "done"})]),
        ])
        plan, _t, _c = planner.plan_with_agent(client, _units(), {}, {}, budget=0, figures=[_figure()])
        # Unit 1 was left uncovered and auto-assigned, also without a count.
        assert [(t.unit_idxs, t.figure_id, t.target_cards) for t in plan.tasks] == [([0], None, 0), ([0], 11, 0), ([1], None, 0)]

    def test_the_structured_fallback_asks_for_no_count_either(self):
        schema = json.dumps(planner.plan_schema(False))
        assert "target_cards" not in schema and '"cards"' in schema
        assert "target_cards" in json.dumps(planner.plan_schema(True))


class TestTheWriters:
    def test_a_task_without_a_count_is_told_none(self):
        task = PlanTask(id=1, unit_idxs=[0], strategy="general", target_cards=0, notes="Cover it.")
        user = workers.build_worker_messages(task, _units(1), {}, "basic")[-1]["content"]
        assert user.startswith("TASK: write cards using the general strategy.") and workers.UNCOUNTED_BRIEF in user
        assert "write about" not in user

    def test_a_counted_task_keeps_its_count(self):
        task = PlanTask(id=1, unit_idxs=[0], strategy="general", target_cards=6, notes="")
        user = workers.build_worker_messages(task, _units(1), {"target_cards": 30}, "basic")[-1]["content"]
        assert user.startswith("TASK: write about 6 cards") and workers.COUNTED_BRIEF in user

    def test_a_cut_off_answer_is_retried_without_a_count(self):
        sent = []

        class Client:
            def chat(self, role, messages, response_format=None):
                sent.append(messages[-1]["content"])
                if len(sent) == 1:
                    return _Result("{\"cards\": [", "length")
                return _Result(json.dumps({"cards": [{"type": "basic", "front": "Q?", "back": "A.", "cloze_text": None,
                                                      "extra": None, "tags": [], "source_quote": "lipid bilayers"}]}))

        out = workers.run_worker(Client(), [{"role": "user", "content": "TASK: write cards using the general strategy."}], 0)
        assert out["attempts"] == 2 and len(out["cards"]) == 1
        assert "cut off before it finished" in sent[1] and not re.search(r"\d+ cards", sent[1])


class _Result:
    def __init__(self, content, finish_reason="stop"):
        self.content, self.finish_reason = content, finish_reason
        self.usage, self.model, self.cost = {"prompt_tokens": 1, "completion_tokens": 1}, "m", 0.0


def test_a_run_on_auto_never_shows_a_model_a_count(app, monkeypatch):
    fake = _install_fake(monkeypatch)
    deck_id = _make_deck(app)
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "ready" and deck.run_json["budget"] == 0
        assert {t["target_cards"] for t in deck.run_json["plan"]["tasks"]} == {0}
        planner_calls = [c for c in fake.calls if c["tools"]]
        writes = [c for c in fake.calls if c["name"] == "anki_cards"]
        assert planner_calls and writes
        for call in planner_calls + writes:
            text = _model_text(call["messages"])
            # The fake planner script itself sends `target_cards` in its own tool calls,
            # which come back in the transcript; what the app wrote must hold no count.
            app_text = "".join(m["content"] or "" for m in call["messages"] if m["role"] in ("system", "user"))
            assert not re.search(r"\b\d+ cards\b|suggested cards|target_cards|estimate", app_text), call["name"]
            assert text


def test_the_plan_review_leaves_an_empty_count_empty(app, client, monkeypatch):
    from app.routes import main

    _install_fake(monkeypatch)
    deck_id = _make_deck(app, settings={"review_plan": True})
    with app.app_context():
        assert _generate(app, monkeypatch, deck_id) == deck_id
        first, second = _db.session.get(Deck, deck_id).run_json["plan"]["tasks"][:2]
    login(client, email="gen@example.com")
    page = client.get(f"/decks/{deck_id}/plan").text
    assert ">no card counts<" in page and "for about" not in page
    monkeypatch.setattr(main, "dispatch_generation", lambda *args, **kwargs: None)
    client.post(f"/decks/{deck_id}/plan", data={"action": "run", f"target_{first['id']}": "", f"target_{second['id']}": "5"})
    with app.app_context():
        tasks = {t["id"]: t for t in _db.session.get(Deck, deck_id).run_json["plan"]["tasks"]}
        # Left empty, no count; typed, the student's own.
        assert tasks[first["id"]]["target_cards"] == 0 and tasks[second["id"]]["target_cards"] == 5
