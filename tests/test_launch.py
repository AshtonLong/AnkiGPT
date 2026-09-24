"""Go-live features: password reset, legal pages, and running behind a proxy."""

import re

import pytest

from app import create_app
from app.extensions import db as _db
from app.routes import auth as auth_routes

from conftest import TestConfig, login, logout, register


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(auth_routes, "send_mail", lambda to, subject, text, html=None: sent.append(
        {"to": to, "subject": subject, "text": text, "html": html}) or True)
    return sent


def _reset_path(message):
    return re.search(r"http://localhost(/auth/reset/\S+)", message["text"]).group(1)


def test_reset_flow_changes_password_and_signs_in(client, outbox):
    register(client)
    logout(client)
    resp = client.post("/auth/forgot", data={"email": "A@example.com"})
    assert b"Check your inbox" in resp.data
    assert len(outbox) == 1 and outbox[0]["to"] == "a@example.com"

    path = _reset_path(outbox[0])
    assert client.get(path).status_code == 200
    resp = client.post(path, data={"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/decks")
    assert client.get("/decks").status_code == 200

    logout(client)
    assert b"Invalid credentials" in login(client, password="password123").data
    assert b"Invalid credentials" not in login(client, password="brand-new-pass").data
    # The link is single-use: it dies with the password it reset.
    logout(client)
    assert client.get(path).status_code == 400


def test_reset_does_not_reveal_unknown_emails(client, outbox):
    resp = client.post("/auth/forgot", data={"email": "nobody@example.com"})
    assert b"Check your inbox" in resp.data
    assert outbox == []


def test_reset_emails_are_throttled(client, outbox):
    register(client)
    logout(client)
    client.post("/auth/forgot", data={"email": "a@example.com"})
    client.post("/auth/forgot", data={"email": "a@example.com"})
    assert len(outbox) == 1


def test_reset_rejects_mismatched_and_short_passwords(client, outbox):
    register(client)
    logout(client)
    client.post("/auth/forgot", data={"email": "a@example.com"})
    path = _reset_path(outbox[0])
    assert client.post(path, data={"new_password": "short", "confirm_password": "short"}).status_code == 422
    resp = client.post(path, data={"new_password": "long-enough-1", "confirm_password": "long-enough-2"})
    assert resp.status_code == 422 and b"match" in resp.data


def test_tampered_reset_token_is_rejected(client):
    assert client.get("/auth/reset/not-a-real-token").status_code == 400


def test_login_links_to_password_reset(client):
    assert b"/auth/forgot" in client.get("/auth/login").data


@pytest.mark.parametrize("path", ["/terms", "/privacy", "/refunds"])
def test_legal_pages_are_public(app, path):
    app.config["SUPPORT_EMAIL"] = "help@example.com"
    resp = app.test_client().get(path)
    assert resp.status_code == 200
    assert b"mailto:help@example.com" in resp.data


def test_signup_and_footer_link_the_policies(client):
    assert b"/terms" in client.get("/auth/signup").data
    landing = client.get("/").data
    assert b"/privacy" in landing and b"/refunds" in landing


def test_external_urls_use_forwarded_https_behind_proxy(tmp_path, monkeypatch):
    class ProxyConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'proxy.db'}"
        PROXY_FIX_HOPS = 1

    sent = []
    monkeypatch.setattr(auth_routes, "send_mail", lambda to, subject, text, html=None: sent.append(text))
    proxied = create_app(ProxyConfig)
    client = proxied.test_client()
    register(client)
    logout(client)
    client.post("/auth/forgot", data={"email": "a@example.com"},
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "ankispark.example", "X-Forwarded-For": "1.2.3.4"})
    assert "https://ankispark.example/auth/reset/" in sent[0]
    with proxied.app_context():
        _db.drop_all()
