import json

import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    AUTH_REQUIRED = True
    WTF_CSRF_ENABLED = False
    OPENROUTER_API_KEY = ""
    GENERATION_IN_THREAD = False
    PIPELINE_MAX_WORKERS = 2
    PIPELINE_CACHE_ENABLED = False


@pytest.fixture
def app(tmp_path):
    db_file = tmp_path / "test.db"
    TestConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_file}"
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with app.app_context():
        yield _db


def register(client, email="a@example.com", password="password123"):
    return client.post(
        "/auth/signup",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


def login(client, email="a@example.com", password="password123"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


def logout(client):
    return client.post("/auth/logout", follow_redirects=True)


# ----------------------------------------------------------------- fake LLM
def fake_response(content, usage=None, finish_reason="stop", tool_calls=None):
    """Shape of an OpenRouter chat-completions response."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
    }


def tool_call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    """Scripted stand-in for `openrouter_chat` that dispatches on the request shape.

    - Mapper (document_map schema) -> groups every candidate into one unit each.
    - Planner (tools present) -> reads unit 0, spawns one task per unit, finishes.
    - Worker (anki_cards schema) -> two cards per call, one deliberately duplicated
      across tasks so the reconcile phase has something to remove.
    - Critic cold pass / judge -> keeps everything except a card flagged unsupported.
    - Duplicate resolution / coverage audit -> canned answers.
    """

    def __init__(self, unit_titles=None):
        self.calls = []
        self.planner_turn = 0

    def __call__(self, messages, model, api_key, *args, **kwargs):
        response_format = kwargs.get("response_format") or {}
        name = (response_format.get("json_schema") or {}).get("name")
        tools = kwargs.get("tools")
        self.calls.append({"name": name, "tools": bool(tools), "model": model, "messages": messages})
        if tools:
            return self._planner(messages)
        handler = getattr(self, f"_{name}", None) if name else None
        if handler is None:
            return fake_response("ok")
        return handler(messages)

    # --- phase 0
    def _document_map(self, messages):
        user = messages[-1]["content"]
        ids = [int(line.split("]")[0][1:]) for line in user.splitlines() if line.startswith("[")]
        units = []
        for i in ids:
            units.append({
                "title": f"Unit {i}", "candidate_ids": [i], "kind": "definitions" if i == 0 else "prose",
                "density": 4, "depends_on": [0] if i else [], "skip": False, "skip_reason": None,
                "summary": f"Covers candidate {i}.",
            })
        return fake_response(json.dumps({"subject": "Biology", "document_summary": "Cells.", "units": units}))

    # --- phase 1
    def _planner(self, messages):
        self.planner_turn += 1
        if self.planner_turn == 1:
            return fake_response(None, tool_calls=[tool_call("c1", "read_unit", {"unit_idx": 0})])
        if self.planner_turn == 2:
            listing = messages[1]["content"]
            idxs = [int(line.split("]")[0][1:]) for line in listing.splitlines() if line.startswith("[")]
            calls = [
                tool_call(f"s{i}", "spawn_task", {"unit_idxs": [i], "strategy": "definition_sweep" if i == 0 else "general",
                                                   "target_cards": 3, "notes": f"Cover unit {i}."})
                for i in idxs
            ]
            return fake_response(None, tool_calls=calls)
        return fake_response(None, tool_calls=[tool_call("f1", "finish_plan", {"summary": "One task per unit."})])

    # --- phase 2
    def _anki_cards(self, messages):
        user = messages[-1]["content"]
        if "Coverage gap-fill" in user:
            cards = [{"type": "basic", "front": "What gap was filled?", "back": "The missing fact.", "cloze_text": None,
                      "extra": None, "tags": ["gap"], "source_quote": "glucose"}]
        else:
            cards = [
                {"type": "basic", "front": "What does photosynthesis produce?", "back": "Glucose", "cloze_text": None,
                 "extra": None, "tags": ["photosynthesis"], "source_quote": "into glucose"},
                {"type": "cloze", "front": None, "back": None, "cloze_text": "Photosynthesis needs {{c1::light}} energy.",
                 "extra": "", "tags": ["photosynthesis"], "source_quote": "using light"},
                {"type": "basic", "front": "Who discovered the Krebs cycle?", "back": "Hans Krebs", "cloze_text": None,
                 "extra": None, "tags": [], "source_quote": None},
            ]
        return fake_response(json.dumps({"cards": cards}))

    # --- phase 3
    def _cold_answers(self, messages):
        n = sum(1 for line in messages[-1]["content"].splitlines() if line.startswith("["))
        return fake_response(json.dumps({"answers": [{"index": i, "answer": "unknown"} for i in range(n)]}))

    def _critic_verdicts(self, messages):
        user = messages[-1]["content"]
        verdicts = []
        for block in user.split("\n\n"):
            if not block.startswith("["):
                continue
            idx = int(block.split("]")[0][1:])
            unsupported = "Krebs" in block
            verdicts.append({
                "index": idx, "supported": not unsupported, "atomic": True, "ambiguous": False, "leaks_answer": False,
                "cold_answer_correct": False, "difficulty": 2, "verdict": "drop" if unsupported else "keep",
                "reason": "not in source" if unsupported else "fine", "rewrite": None,
            })
        return fake_response(json.dumps({"verdicts": verdicts}))

    # --- phase 4
    def _duplicate_resolution(self, messages):
        user = messages[-1]["content"]
        clusters = []
        for block in user.split("\n\n"):
            if not block.startswith("Cluster"):
                continue
            ci = int(block.split(":")[0].split()[1])
            first = int(block.splitlines()[1].strip().split("]")[0][1:])
            clusters.append({"cluster": ci, "keep": [first], "reason": "same fact"})
        return fake_response(json.dumps({"clusters": clusters}))

    def _coverage_audit(self, messages):
        return fake_response(json.dumps({"coverage_score": 80, "missing": [
            {"fact": "Water is split during the light reactions.", "importance": 3, "source_quote": "water"},
        ]}))

    def _improved_basic_card(self, messages):
        return fake_response(json.dumps({"front": "Improved front?", "back": "Improved back."}))

    # --- coach (review feedback)
    def _diagnosed_cards(self, messages):
        user = messages[-1]["content"]
        n = sum(1 for line in user.splitlines() if line.startswith("["))
        cards = []
        for i in range(n):
            if i == 0:
                cards.append({"index": i, "diagnosis": "two facts", "action": "split", "replacements": [
                    {"type": "basic", "front": "Split A?", "back": "a", "cloze_text": None, "extra": None},
                    {"type": "basic", "front": "Split B?", "back": "b", "cloze_text": None, "extra": None},
                ]})
            else:
                cards.append({"index": i, "diagnosis": "ambiguous", "action": "rewrite", "replacements": [
                    {"type": "basic", "front": f"Clearer question {i}?", "back": "answer", "cloze_text": None, "extra": None},
                ]})
        return fake_response(json.dumps({"cards": cards}))


def fake_embeddings(texts, *args, **kwargs):
    """Identical texts -> identical vectors; everything else orthogonal-ish."""
    vectors = []
    for text in texts:
        seed = sum(ord(c) for c in text) % 97
        vec = [0.0] * 8
        vec[seed % 8] = 1.0
        vec[(seed * 7) % 8] += 0.3
        vectors.append(vec)
    return vectors
