"""Desktop mode: one local user, no sign-in, a guarded loopback server (desktop-app/SPEC.md)."""

import io
import json
import os
import re
import sqlite3
import threading
import zipfile

import pytest
import requests
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

from app import desktop, tasks
from app.config import Config, DesktopConfig
from app.desktop import (
    INTERRUPTED_MESSAGE, LOCAL_USER_EMAIL, TOKEN_HEADER, backup_before_upgrade, create_desktop_app,
    read_settings_env, set_port,
)
from app.extensions import db as _db
from app.models import Card, Deck, Figure, PipelineTask, Source, User
from app.services import llm as llm_module
from app.services.credentials import user_key
from app.services.llm import MissingAPIKeyError, OpenRouterConnectionError, OpenRouterError
from app.services.pipeline import format_generation_error

from conftest import FakeLLM, fake_embeddings, register

TOKEN = "launch-token-" + "a1" * 26
PORT = 53817
HOST = f"127.0.0.1:{PORT}"
KEY = "sk-or-v1-" + "d" * 40
SOURCE = """# Photosynthesis

Photosynthesis converts CO2 and water into glucose using light. Water is split during
the light reactions, releasing oxygen.

# Respiration

Cellular respiration oxidises glucose to release energy stored as ATP. It happens in the
mitochondria of eukaryotic cells and consumes oxygen."""


class WindowClient(FlaskClient):
    """Sends what the Electron window sends: the launch token, to the loopback host and port."""

    def open(self, *args, **kwargs):
        # A followed redirect re-enters with a ready-made request, which already carries both.
        if args and isinstance(args[0], str):
            kwargs.setdefault("base_url", f"http://{HOST}")
            headers = Headers(kwargs.pop("headers", None))
            if TOKEN_HEADER not in headers:
                headers[TOKEN_HEADER] = TOKEN
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


_launched = []


def launch(data_dir, version="1.0.0", **kwargs):
    application = create_desktop_app(str(data_dir), "install-secret", token=TOKEN, version=version, **kwargs)
    set_port(application, PORT)
    application.config.update(
        TESTING=True, WTF_CSRF_ENABLED=False, GENERATION_IN_THREAD=False, PIPELINE_MAX_WORKERS=2,
        PIPELINE_CACHE_ENABLED=False,
    )
    application.test_client_class = WindowClient
    _launched.append(application)
    return application


def close(application):
    with application.app_context():
        _db.session.remove()
        _db.engine.dispose()


@pytest.fixture(autouse=True)
def _close_launched_apps():
    yield
    while _launched:
        close(_launched.pop())


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "AnkiGPT" / "data"


@pytest.fixture
def app(data_dir):
    return launch(data_dir)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_module, "openrouter_chat", fake)
    monkeypatch.setattr(llm_module, "openrouter_embeddings", fake_embeddings)
    return fake


def save_key(client):
    return client.post("/auth/profile", data={"section": "api-key", "openrouter_api_key": KEY})


def make_deck(app, status="ready", title="Cells", tasks=()):
    with app.app_context():
        user = User.query.filter_by(email=LOCAL_USER_EMAIL).one()
        deck = Deck(user_id=user.id, title=title, card_style="basic", status=status, source_type="text",
                    source_text=SOURCE, settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.flush()
        for task_status in tasks:
            _db.session.add(PipelineTask(deck_id=deck.id, phase="write", kind="task", label="t", status=task_status))
        _db.session.add(Card(deck_id=deck.id, type="basic", front="Q?", back="A.", status="ok"))
        _db.session.commit()
        return deck.id


# ------------------------------------------------------------------ no sign-in
def test_first_launch_shows_the_library_with_no_sign_in(client):
    response = client.get("/decks")
    assert response.status_code == 200
    assert b"No decks yet" in response.data
    assert b"Sign in" not in response.data and b"Sign out" not in response.data


def test_every_workspace_route_works_with_no_sign_in(client, app, fake_llm):
    assert client.get("/decks/new").status_code == 200
    assert save_key(client).status_code == 303

    created = client.post("/decks/new", data={"title": "Cells", "source_type": "text", "text_input": SOURCE})
    assert created.status_code == 302
    with app.app_context():
        deck = Deck.query.one()
        deck_id = deck.id
        assert deck.user.email == LOCAL_USER_EMAIL
    assert created.headers["Location"].endswith(f"/decks/{deck_id}/preview")
    assert client.get(f"/decks/{deck_id}/preview").status_code == 200

    # Plan review on: the run pauses at the plan, then resumes from it.
    planned = client.post(f"/decks/{deck_id}/preview", data={"review_plan": "on"})
    assert planned.headers["Location"].endswith(f"/decks/{deck_id}/status")
    assert client.get(f"/decks/{deck_id}/status").headers["Location"].endswith(f"/decks/{deck_id}/plan")
    assert client.get(f"/decks/{deck_id}/plan").status_code == 200
    assert client.post(f"/decks/{deck_id}/plan", data={"action": "run"}).status_code == 302

    assert client.get(f"/decks/{deck_id}/status").status_code == 200
    progress = client.get(f"/decks/{deck_id}/progress.json")
    assert progress.status_code == 200 and progress.get_json()["status"] == "ready"
    for view in ("", "?view=insights", "?view=coach"):
        assert client.get(f"/decks/{deck_id}{view}").status_code == 200

    with app.app_context():
        card = Card.query.filter_by(deck_id=deck_id, type="basic", status="ok").first()
        card_id = card.id
    assert client.post(f"/cards/{card_id}", data={"front": "Edited?", "back": "Yes.", "tags": "a"}).status_code == 200
    assert b"Improved front?" in client.post(f"/cards/{card_id}/improve").data
    tagged = client.post("/cards/bulk", data={"action": "tag", "tag": "exam", "card_ids": [card_id]})
    assert tagged.status_code == 302

    exported = client.post(f"/decks/{deck_id}/export")
    assert exported.status_code == 200
    assert "collection.anki2" in zipfile.ZipFile(io.BytesIO(exported.data)).namelist()
    imported = client.post(
        f"/decks/{deck_id}/reviews", data={"anki_package": (io.BytesIO(exported.data), "cells.apkg")},
        headers={"Accept": "application/json"},
    )
    assert imported.status_code in (200, 422) and imported.get_json()["message"]
    assert client.post(f"/decks/{deck_id}/coach").status_code == 302

    with app.app_context():
        figure = Figure(deck_id=deck_id, page=1, hash="h", image=b"\x89PNG", mime="image/png")
        _db.session.add(figure)
        _db.session.commit()
        figure_id = figure.id
    assert client.get(f"/figures/{figure_id}.png").data == b"\x89PNG"

    assert client.get("/auth/profile").status_code == 200
    assert client.post(f"/decks/{deck_id}/delete").status_code == 302
    with app.app_context():
        assert Deck.query.count() == 0


def test_csrf_protection_stays_on(app):
    app.config["WTF_CSRF_ENABLED"] = True
    response = app.test_client().post("/decks/new", data={"title": "x", "source_type": "text", "text_input": "y"})
    assert response.status_code == 400


# ------------------------------------------------- account and legal pages
@pytest.mark.parametrize("path", ["/", "/auth/signup", "/auth/login", "/auth/forgot", "/auth/reset/some-token"])
def test_account_pages_redirect_to_the_library(client, path):
    response = client.get(path)
    assert response.status_code == 302 and response.headers["Location"].endswith("/decks")


@pytest.mark.parametrize("path", ["/auth/signup", "/auth/login", "/auth/forgot", "/auth/logout"])
def test_account_actions_do_nothing(client, app, path):
    response = client.post(path, data={"email": "new@example.com", "password": "password123"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/decks")
    assert client.get("/decks").status_code == 200
    with app.app_context():
        assert [user.email for user in User.query.all()] == [LOCAL_USER_EMAIL]


@pytest.mark.parametrize("path", ["/terms", "/privacy"])
def test_legal_pages_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_pages_have_no_account_controls_or_footer(client, app):
    deck_id = make_deck(app)
    for path in ("/decks", "/decks/new", f"/decks/{deck_id}", "/auth/profile"):
        html = client.get(path).data
        for absent in (b"/auth/login", b"/auth/signup", b"/auth/logout", b"/terms", b"/privacy",
                       b'<footer class="site"', b"My profile", b"Sign out", b'class="account"'):
            assert absent not in html, (path, absent)
        assert b"Local workspace" in html and b"Settings" in html


# --------------------------------------------------------------- request guard
@pytest.mark.parametrize("path", ["/decks", "/static/style.css", "/static/vendor/htmx.min.js", "/auth/profile", "/nope"])
def test_requests_from_outside_the_window_are_refused(app, path):
    plain = FlaskClient(app)
    here = f"http://{HOST}"
    assert plain.get(path, base_url=here).status_code == 403
    assert plain.get(path, base_url=here, headers={TOKEN_HEADER: "wrong"}).status_code == 403
    assert plain.get(path, base_url=here, headers={TOKEN_HEADER: TOKEN[:-1]}).status_code == 403
    for host in ("localhost", f"localhost:{PORT}", "127.0.0.1", f"127.0.0.1:{PORT + 1}", f"evil.example:{PORT}"):
        assert plain.get(path, base_url=f"http://{host}", headers={TOKEN_HEADER: TOKEN}).status_code == 403, host
    allowed = plain.get(path, base_url=here, headers={TOKEN_HEADER: TOKEN})
    assert allowed.status_code == (404 if path == "/nope" else 200)


def test_refused_requests_never_reach_the_app(app):
    response = FlaskClient(app).post("/decks/new", base_url=f"http://{HOST}",
                                     data={"title": "x", "source_type": "text", "text_input": "y"})
    assert response.status_code == 403 and response.data == b"Forbidden"
    with app.app_context():
        assert Deck.query.count() == 0


def test_nothing_is_served_before_the_port_is_known(data_dir):
    application = create_desktop_app(str(data_dir), "install-secret", token=TOKEN, version="1.0.0")
    _launched.append(application)
    response = FlaskClient(application).get("/decks", base_url=f"http://{HOST}", headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 403


def test_dev_mode_skips_the_token_but_still_checks_the_host(data_dir):
    application = launch(data_dir, check_token=False)
    plain = FlaskClient(application)
    assert plain.get("/decks", base_url=f"http://{HOST}").status_code == 200
    assert plain.get("/decks", base_url=f"http://evil.example:{PORT}").status_code == 403


def test_the_web_app_has_no_guard_and_no_local_user(tmp_path):
    from app import create_app

    class WebConfig(Config):
        TESTING = True
        SECRET_KEY = "web"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'web.db'}"

    web = create_app(WebConfig)
    _launched.append(web)
    client = web.test_client()
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/").status_code == 200 and client.get("/terms").status_code == 200
    assert "/auth/login" in client.get("/decks").headers["Location"]
    with web.app_context():
        assert User.query.count() == 0
        assert not web.config.get("DESKTOP_MODE")


# ------------------------------------------------------------------ local user
def test_the_local_user_is_created_once(data_dir):
    first = launch(data_dir)
    with first.app_context():
        user = User.query.one()
        assert user.email == LOCAL_USER_EMAIL
        for guess in ("", "password", user.password_hash):
            assert not user.check_password(guess)
        user_id = user.id
    close(first)

    second = launch(data_dir)
    with second.app_context():
        assert [user.id for user in User.query.all()] == [user_id]


def test_decks_survive_a_relaunch(data_dir):
    first = launch(data_dir)
    deck_id = make_deck(first, title="Kept")
    close(first)
    assert b"Kept" in launch(data_dir).test_client().get(f"/decks/{deck_id}").data


# ------------------------------------------------------------- interrupted runs
def test_interrupted_runs_are_marked_failed_at_start(data_dir):
    first = launch(data_dir)
    interrupted = make_deck(first, status="processing", tasks=("done", "running", "queued"))
    planned = make_deck(first, status="planned", tasks=("done",))
    ready = make_deck(first, status="ready", tasks=("done", "cached"))
    draft = make_deck(first, status="draft")
    failed = make_deck(first, status="failed")
    with first.app_context():
        deck = _db.session.get(Deck, failed)
        deck.run_json = {"last_error": "An earlier failure."}
        _db.session.commit()
    close(first)

    second = launch(data_dir)
    with second.app_context():
        deck = _db.session.get(Deck, interrupted)
        assert deck.status == "failed" and deck.run_json["last_error"] == INTERRUPTED_MESSAGE
        assert sorted(task.status for task in deck.tasks) == ["done", "failed", "failed"]
        assert _db.session.get(Deck, planned).status == "planned"
        assert _db.session.get(Deck, ready).status == "ready"
        assert sorted(task.status for task in _db.session.get(Deck, ready).tasks) == ["cached", "done"]
        assert _db.session.get(Deck, draft).status == "draft"
        assert _db.session.get(Deck, failed).run_json == {"last_error": "An earlier failure."}
    page = second.test_client().get(f"/decks/{interrupted}/status").data
    assert INTERRUPTED_MESSAGE.encode() in page and b"Retry generation" in page


def test_the_web_app_leaves_processing_decks_alone(tmp_path):
    from app import create_app

    class WebConfig(Config):
        TESTING = True
        SECRET_KEY = "web"
        WTF_CSRF_ENABLED = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'web.db'}"

    first = create_app(WebConfig)
    _launched.append(first)
    register(first.test_client())
    with first.app_context():
        deck = Deck(user_id=User.query.one().id, title="Live", card_style="basic", status="processing",
                    source_type="text", source_text="x")
        _db.session.add(deck)
        _db.session.commit()
    close(first)
    second = create_app(WebConfig)
    _launched.append(second)
    with second.app_context():
        assert Deck.query.one().status == "processing"


# ------------------------------------------------------------------ data folder
def test_everything_lands_in_the_data_folder(data_dir, app, client):
    assert app.instance_path == str(data_dir)
    assert (data_dir / "ankigpt.db").is_file()
    assert (data_dir / "uploads").is_dir()
    assert (data_dir / "version.txt").read_text() == "1.0.0"
    assert app.config["UPLOAD_FOLDER"] == str(data_dir / "uploads")
    save_key(client)
    with sqlite3.connect(data_dir / "ankigpt.db") as connection:
        assert connection.execute("SELECT email, openrouter_key_hint FROM user").fetchall() == [
            (LOCAL_USER_EMAIL, KEY[-4:])]
    connection.close()


def test_desktop_config_fixes_its_values(app):
    config = app.config
    assert config["DESKTOP_MODE"] is True and config["SECRET_KEY"] == "install-secret"
    assert config["OPENROUTER_API_KEY"] == "" and "github.com" in config["OPENROUTER_SITE_URL"]
    assert config["UPLOAD_MAX_MB"] == 200 and config["MAX_CONTENT_LENGTH"] == 200 * 1024 * 1024
    assert config["PROXY_FIX_HOPS"] == 0 and config["SESSION_COOKIE_SECURE"] is False
    assert not any(config[key] for key in ("MAIL_SMTP_HOST", "MAIL_SMTP_USERNAME", "MAIL_SMTP_PASSWORD", "MAIL_FROM"))
    assert DesktopConfig.GENERATION_IN_THREAD is True
    assert not hasattr(Config, "DESKTOP_MODE")


@pytest.mark.parametrize("secret", ["", None])
def test_the_backend_refuses_to_start_without_the_install_secret(data_dir, secret):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_desktop_app(str(data_dir), secret, token=TOKEN, version="1.0.0")


# ---------------------------------------------------------------- settings.env
def test_settings_env_applies_listed_keys_and_ignores_the_rest(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "settings.env").write_text(
        "# Advanced settings\n"
        "OPENROUTER_MODEL=vendor/next-model\n"
        'OPENROUTER_MODEL_PLANNER="vendor/planner"\n'
        "OPENROUTER_REASONING_WORKER=high\n"
        "OPENROUTER_EMBEDDING_MODEL=vendor/embed\n"
        "OPENROUTER_TEMPERATURE=0.2\n"
        "OPENROUTER_TIMEOUT_SECONDS=45.5\n"
        "OPENROUTER_MAX_TOKENS=9000\n"
        "PIPELINE_MAX_WORKERS=3\n"
        "PIPELINE_CRITIC_ENABLED=false\n"
        "MAX_SOURCE_CHARS=1234\n"
        # Not on the list, or not a real setting, or not a valid value.
        "OPENROUTER_API_KEY=sk-or-v1-pasted-by-mistake\n"
        "SECRET_KEY=overridden\n"
        "DATABASE_URL=sqlite:///elsewhere.db\n"
        "UPLOAD_MAX_MB=1\n"
        "OPENROUTER_SITE_URL=https://example.com\n"
        "PIPELINE_NOT_A_SETTING=1\n"
        "PIPELINE_PLANNER_MAX_TURNS=many\n",
        encoding="utf-8",
    )
    settings, ignored = read_settings_env(data_dir / "settings.env")
    assert sorted(ignored) == [
        "DATABASE_URL", "OPENROUTER_API_KEY", "OPENROUTER_SITE_URL", "PIPELINE_NOT_A_SETTING",
        "PIPELINE_PLANNER_MAX_TURNS", "SECRET_KEY", "UPLOAD_MAX_MB",
    ]

    config = launch(data_dir).config
    assert config["OPENROUTER_MODEL"] == "vendor/next-model"
    assert config["OPENROUTER_MODEL_PLANNER"] == "vendor/planner"
    assert config["OPENROUTER_REASONING_WORKER"] == "high"
    assert config["OPENROUTER_EMBEDDING_MODEL"] == "vendor/embed"
    assert config["OPENROUTER_TEMPERATURE"] == "0.2"
    assert config["OPENROUTER_TIMEOUT_SECONDS"] == 45.5
    assert config["OPENROUTER_MAX_TOKENS"] == 9000
    assert config["PIPELINE_CRITIC_ENABLED"] is False
    assert config["MAX_SOURCE_CHARS"] == 1234
    assert settings["PIPELINE_MAX_WORKERS"] == 3
    # The rest keep their desktop values.
    assert config["OPENROUTER_API_KEY"] == "" and config["SECRET_KEY"] == "install-secret"
    assert config["UPLOAD_MAX_MB"] == 200 and "github.com" in config["OPENROUTER_SITE_URL"]
    assert config["PIPELINE_PLANNER_MAX_TURNS"] == Config.PIPELINE_PLANNER_MAX_TURNS
    assert config["SQLALCHEMY_DATABASE_URI"].database == str(data_dir / "ankigpt.db").replace("\\", "/")


def test_ignored_settings_are_logged_by_name_only(data_dir, caplog):
    data_dir.mkdir(parents=True)
    (data_dir / "settings.env").write_text("OPENROUTER_API_KEY=sk-or-v1-pasted-by-mistake\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="app.desktop"):
        launch(data_dir)
    assert "OPENROUTER_API_KEY" in caplog.text and "pasted-by-mistake" not in caplog.text


def test_a_missing_settings_file_changes_nothing(data_dir, app):
    assert read_settings_env(data_dir / "settings.env") == ({}, [])
    assert app.config["OPENROUTER_MODEL"] == Config.OPENROUTER_MODEL


# --------------------------------------------------------------------- backups
def test_a_version_change_backs_up_the_database(data_dir):
    first = launch(data_dir, version="1.0.0")
    make_deck(first, title="Before the upgrade")
    close(first)
    assert not (data_dir / "backups").exists()

    close(launch(data_dir, version="1.0.0"))
    assert not (data_dir / "backups").exists()

    close(launch(data_dir, version="1.1.0"))
    (backup,) = (data_dir / "backups").iterdir()
    assert backup.name.startswith("ankigpt-1.0.0-") and backup.suffix == ".db"
    assert (data_dir / "version.txt").read_text() == "1.1.0"
    connection = sqlite3.connect(backup)
    assert connection.execute("SELECT title FROM deck").fetchall() == [("Before the upgrade",)]
    connection.close()


def test_only_the_three_newest_backups_are_kept(data_dir):
    close(launch(data_dir, version="1.0.0"))
    for number in range(1, 6):
        backup = backup_before_upgrade(str(data_dir), f"1.0.{number}")
        # Give each copy its own age, oldest first, whatever the clock's resolution.
        stamp = 1_700_000_000 + number * 86400
        os.utime(backup, (stamp, stamp))
    names = sorted(path.name.rsplit("-", 2)[0] for path in (data_dir / "backups").iterdir())
    assert names == ["ankigpt-1.0.2", "ankigpt-1.0.3", "ankigpt-1.0.4"]


def test_a_first_launch_writes_the_version_and_makes_no_backup(tmp_path):
    assert backup_before_upgrade(str(tmp_path), "1.0.0") is None
    assert (tmp_path / "version.txt").read_text() == "1.0.0" and not (tmp_path / "backups").exists()


def test_a_database_with_no_recorded_version_is_backed_up(data_dir):
    close(launch(data_dir, version="1.0.0"))
    (data_dir / "version.txt").unlink()
    backup = backup_before_upgrade(str(data_dir), "1.0.0")
    assert os.path.basename(backup).startswith("ankigpt-unknown-")


# --------------------------------------------------------------- settings page
def test_settings_page_has_the_key_advanced_data_and_about_panels(client, app, data_dir):
    html = client.get("/auth/profile").get_data(as_text=True)
    assert "<title>Settings · AnkiGPT</title>" in html
    for panel in ('id="api-key"', 'id="advanced"', 'id="your-data"', 'id="about"'):
        assert panel in html
    assert str(data_dir) in html
    assert "sent to OpenRouter" in html and "Nothing is sent to an AnkiGPT server" in html
    assert "GitHub to check for updates" in html
    assert 'class="settings-version">1.0.0<' in html
    assert 'href="https://openrouter.ai/keys" target="_blank"' in html
    for absent in ("display_name", "avatar_color", 'name="bio"', 'name="email"', "current_password",
                   "new_password", "shared key", 'id="password"'):
        assert absent not in html, absent


def test_the_key_is_saved_replaced_and_removed_from_settings(client, app):
    assert "No key yet" in client.get("/auth/profile").get_data(as_text=True)
    assert "Add your OpenRouter key." in client.get("/decks").get_data(as_text=True)

    saved = save_key(client)
    assert saved.status_code == 303 and saved.headers["Location"].endswith("/auth/profile#api-key")
    html = client.get("/auth/profile").get_data(as_text=True)
    assert f"A key ending in <code>{KEY[-4:]}</code> is saved" in html and KEY not in html
    with app.app_context():
        user = User.query.one()
        assert user_key(user) == KEY and KEY not in user.openrouter_key_encrypted

    rejected = client.post("/auth/profile", data={"section": "api-key", "openrouter_api_key": "not a key"})
    assert rejected.status_code == 422 and b"look like an OpenRouter key" in rejected.data
    client.post("/auth/profile", data={"section": "api-key", "action": "remove"})
    with app.app_context():
        assert User.query.one().openrouter_key_encrypted is None


def test_agent_effort_is_set_and_reset_from_settings(client, app):
    saved = client.post("/auth/profile", data={"section": "advanced", "effort_judge": "4", "effort_coach": "5"})
    assert saved.status_code == 303 and saved.headers["Location"].endswith("/auth/profile#advanced")
    with app.app_context():
        assert User.query.one().agent_efforts_json == {"judge": "high", "coach": "xhigh"}
    html = client.get("/auth/profile").get_data(as_text=True)
    assert '<output for="effort_judge">High</output>' in html

    rejected = client.post("/auth/profile", data={"section": "advanced", "effort_judge": "9"})
    assert rejected.status_code == 422 and b"Set each agent" in rejected.data
    client.post("/auth/profile", data={"section": "advanced", "action": "reset"})
    with app.app_context():
        assert User.query.one().agent_efforts_json is None


def test_a_default_set_in_settings_env_is_the_one_the_sliders_name(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "settings.env").write_text("OPENROUTER_REASONING_CRITIC=high\nOPENROUTER_REASONING_WORKER=\n",
                                           encoding="utf-8")
    html = launch(data_dir).test_client().get("/auth/profile").get_data(as_text=True)
    assert '<output for="effort_judge">Default · High</output>' in html
    assert '<output for="effort_coach">Default · High</output>' in html
    # An emptied default sends no effort, so there is no level to name.
    assert '<output for="effort_worker">Default</output>' in html
    assert '<output for="effort_planner">Default · Medium</output>' in html


def test_a_key_saved_under_another_install_secret_is_asked_for_again(data_dir):
    first = launch(data_dir)
    save_key(first.test_client())
    close(first)
    second = create_desktop_app(str(data_dir), "a-new-install-secret", token=TOKEN, version="1.0.0")
    set_port(second, PORT)
    second.config["WTF_CSRF_ENABLED"] = False
    second.test_client_class = WindowClient
    _launched.append(second)
    assert b"can no longer be read" in second.test_client().get("/auth/profile").data


@pytest.mark.parametrize("section", ["profile", "email", "password", ""])
def test_account_settings_cannot_be_changed(client, app, section):
    response = client.post("/auth/profile", data={
        "section": section, "display_name": "Mallory", "bio": "x", "avatar_color": "sage",
        "email": "new@example.com", "email_password": "x", "current_password": "x",
        "new_password": "password123", "confirm_password": "password123",
    })
    assert response.status_code == 422 and b"Choose a setting to update." in response.data
    with app.app_context():
        user = User.query.one()
        assert (user.email, user.display_name, user.avatar_color) == (LOCAL_USER_EMAIL, None, None)


# ------------------------------------------------------- wording and bundled assets
def test_messages_name_settings_as_the_place_for_the_key(client, app):
    deck_id = make_deck(app, status="draft")
    preview = client.get(f"/decks/{deck_id}/preview").get_data(as_text=True)
    assert "Add it in Settings" in preview and "My profile" not in preview
    refused = client.post(f"/decks/{deck_id}/preview", data={}, follow_redirects=True)
    assert b"Add your OpenRouter API key under Settings before generating." in refused.data
    with app.app_context():
        card_id = Card.query.filter_by(deck_id=deck_id).one().id
    improve = client.post(f"/cards/{card_id}/improve")
    assert "under Settings to use AI improve" in json.loads(improve.headers["HX-Trigger"])["improveError"]["message"]
    with app.app_context():
        assert str(MissingAPIKeyError()) == "No OpenRouter API key. Add yours under Settings, then try again."
        assert "under Settings" in format_generation_error(OpenRouterError("no", status_code=401))
    # Outside the desktop app the same messages keep the web app's name for the page.
    assert "under My profile" in str(MissingAPIKeyError())


def test_export_tells_desktop_users_to_choose_where_to_save(client, app):
    html = client.get(f"/decks/{make_deck(app)}").get_data(as_text=True)
    assert 'data-ready-message="Choose where to save your package, then open it in Anki."' in html


def test_fonts_and_htmx_are_served_by_the_app(client):
    html = client.get("/decks").get_data(as_text=True)
    for remote in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net", "https://unpkg.com"):
        assert remote not in html
    assert '/static/fonts.css"' in html and '/static/vendor/htmx.min.js"' in html
    stylesheet = client.get("/static/fonts.css").get_data(as_text=True)
    assert "https://" not in stylesheet
    for name in ("figtree-latin", "figtree-latin-ext", "fragment-mono-latin", "fragment-mono-latin-ext",
                 "fragment-mono-cyrillic-ext"):
        assert f'url("fonts/{name}.woff2")' in stylesheet
        assert client.get(f"/static/fonts/{name}.woff2").data[:4] == b"wOF2"
    assert b"htmx" in client.get("/static/vendor/htmx.min.js").data[:400]
    for licence in ("fonts/Figtree-OFL.txt", "fonts/FragmentMono-OFL.txt", "vendor/htmx-LICENSE.txt"):
        assert client.get(f"/static/{licence}").status_code == 200


def test_the_cheat_sheet_saves_as_a_pdf_and_typesets_maths_offline(client, app):
    deck_id = make_deck(app)
    with app.app_context():
        _db.session.get(Deck, deck_id).run_json = {"plan": {"tasks": []}, "cheat_sheet": {"units": 1}}
        _db.session.add(Source(deck_id=deck_id, idx=0, title="Motion", text=r"- Speed: \(v = d / t\)", hash="a"))
        _db.session.commit()
    html = client.get(f"/decks/{deck_id}/cheat-sheet").get_data(as_text=True)
    # The shell writes the PDF when this button is pressed; there is no print dialog to offer.
    assert "data-print>" in html and "Save as PDF" in html and "Print or save" not in html
    assert r'<span class="tex" data-tex="v = d / t">\(v = d / t\)</span>' in html
    for remote in ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "https://unpkg.com"):
        assert remote not in html
    assert '/static/vendor/katex/katex.min.js"' in html and '/static/sheet.js"' in html
    assert b"katex" in client.get("/static/vendor/katex/katex.min.js").data[:400]
    stylesheet = client.get("/static/vendor/katex/katex.min.css").get_data(as_text=True)
    assert "https://" not in stylesheet
    # A browser takes the first format it can read, which is always the .woff2.
    fonts = set(re.findall(r"url\(fonts/([\w-]+\.woff2)\)", stylesheet))
    assert len(fonts) == 20
    for name in fonts:
        assert client.get(f"/static/vendor/katex/fonts/{name}").data[:4] == b"wOF2"
    assert client.get("/static/vendor/katex/LICENSE.txt").status_code == 200


# -------------------------------------------------------------- offline errors
def _offline(*args, **kwargs):
    raise requests.ConnectionError("HTTPSConnectionPool(host='openrouter.ai', port=443): Max retries exceeded")


def test_connection_failures_get_a_plain_message(monkeypatch):
    monkeypatch.setattr(llm_module.requests, "post", _offline)
    with pytest.raises(OpenRouterConnectionError) as raised:
        llm_module.openrouter_chat([], "m", KEY, max_retries=0)
    message = format_generation_error(raised.value)
    assert message == "Couldn't reach OpenRouter. Check your internet connection, then retry."
    # A slow answer is not a missing connection, and keeps its own wording.
    monkeypatch.setattr(llm_module.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ReadTimeout("slow")))
    with pytest.raises(OpenRouterError) as raised:
        llm_module.openrouter_chat([], "m", KEY, max_retries=0)
    assert not isinstance(raised.value, OpenRouterConnectionError)
    assert format_generation_error(raised.value) == "OpenRouter request failed: slow"


def test_generating_offline_fails_with_the_connection_message(client, app, monkeypatch):
    monkeypatch.setattr(llm_module.requests, "post", _offline)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    save_key(client)
    client.post("/decks/new", data={"title": "Cells", "source_type": "text", "text_input": SOURCE})
    with app.app_context():
        deck_id = Deck.query.one().id
    client.post(f"/decks/{deck_id}/preview", data={})
    with app.app_context():
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "failed"
        assert deck.run_json["last_error"] == "Couldn't reach OpenRouter. Check your internet connection, then retry."
    # The app itself keeps working with no connection.
    assert client.get("/decks").status_code == 200 and client.get(f"/decks/{deck_id}/status").status_code == 200


# ----------------------------------------------------------- activity reporting
def test_generation_threads_report_activity_to_the_shell(app, capsys, monkeypatch):
    from app.services import pipeline

    started, release = threading.Event(), threading.Event()

    def generate(deck_id, resume_from_plan=False):
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(pipeline, "generate_deck", generate)
    app.config["GENERATION_IN_THREAD"] = True
    with app.app_context():
        tasks.dispatch_generation(1)
        tasks.dispatch_generation(2)
    assert started.wait(10)
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith("ankigpt-gen-"):
            thread.join(10)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["activity"] * 4
    assert [line["active_runs"] for line in lines] == [1, 2, 1, 0]


def test_the_web_app_reports_nothing(tmp_path, capsys, monkeypatch):
    from app import create_app
    from app.services import pipeline

    class WebConfig(Config):
        TESTING = True
        SECRET_KEY = "web"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'web.db'}"

    web = create_app(WebConfig)
    _launched.append(web)
    monkeypatch.setattr(pipeline, "generate_deck", lambda deck_id, resume_from_plan=False: None)
    with web.app_context():
        tasks.dispatch_generation(1)
    for thread in threading.enumerate():
        if thread.name.startswith("ankigpt-gen-"):
            thread.join(10)
    assert capsys.readouterr().out == ""


def test_messages_to_the_shell_are_single_json_lines(capsys):
    desktop.emit("ready", port=53817)
    assert capsys.readouterr().out == '{"event":"ready","port":53817}\n'
