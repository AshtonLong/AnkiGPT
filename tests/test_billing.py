import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pytest

from app.extensions import db as _db
from app.models import Deck, UsageRecord, User
from app.routes import billing as billing_routes
from app.routes import main as main_routes
from app.services import billing

from conftest import register

WEBHOOK_SECRET = "whsec_test"


@pytest.fixture
def paid_app(app, monkeypatch):
    app.config.update(BILLING_ENABLED=True, STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    # Metering is what's under test; don't run the pipeline.
    monkeypatch.setattr(main_routes, "dispatch_generation", lambda *a, **k: None)
    return app


@pytest.fixture
def paid_client(paid_app):
    return paid_app.test_client()


def _user(app, email="a@example.com"):
    with app.app_context():
        return User.query.filter_by(email=email).one().id


def _deck(app, user_id, chars, title="Chapter"):
    with app.app_context():
        deck = Deck(user_id=user_id, title=title, card_style="basic", status="draft",
                    source_type="text", source_text="x" * chars, settings_json={}, run_json={})
        _db.session.add(deck)
        _db.session.commit()
        return deck.id


def _generate(client, deck_id):
    return client.post(f"/decks/{deck_id}/preview", data={"target_cards": "auto"})


def _usage(app, user_id):
    with app.app_context():
        return [r.pages for r in UsageRecord.query.filter_by(user_id=user_id).order_by(UsageRecord.id)]


def _subscription(sub_id="sub_1", status="active", plan="pro", interval="month", **extra):
    period_end = int(time.time()) + 30 * 86400
    return {
        "id": sub_id, "status": status, "customer": "cus_1", "cancel_at_period_end": False,
        "items": {"data": [{"id": "si_1", "current_period_end": period_end,
                            "price": {"lookup_key": f"ankigpt_{plan}_{interval}ly"}}]},
        **extra,
    }


def _signed(payload, secret=WEBHOOK_SECRET):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def _post_event(client, event_type, obj):
    payload = json.dumps({"id": "evt_1", "type": event_type, "data": {"object": obj}})
    return client.post("/billing/webhook", data=payload, headers=_signed(payload))


# ------------------------------------------------------------------ plans & periods
def test_pages_round_up_by_characters():
    assert billing.pages_for(0) == 1
    assert billing.pages_for(4000) == 1
    assert billing.pages_for(4001) == 2


def test_period_rolls_over_the_year():
    start, end = billing.period_bounds(datetime(2026, 12, 15, 9, 30))
    assert (start, end) == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_paid_plan_only_counts_while_subscription_is_live():
    user = User(email="x@example.com", plan="max", subscription_status="past_due")
    assert billing.plan_for(user).key == "max"
    user.subscription_status = "canceled"
    assert billing.plan_for(user).key == "free"
    user.plan, user.subscription_status = "bogus", "active"
    assert billing.plan_for(user).key == "free"


def test_pricing_page_is_public(paid_client):
    resp = paid_client.get("/pricing")
    assert resp.status_code == 200
    assert b"Most popular" in resp.data and b"$</span><b data-price" in resp.data


def test_billing_page_requires_sign_in(paid_client):
    resp = paid_client.get("/billing")
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]
    register(paid_client)
    page = paid_client.get("/billing")
    assert page.status_code == 200
    assert b"re on Free." in page.data


# ------------------------------------------------------------------ metering
def test_generation_is_metered_by_pages(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    deck_id = _deck(paid_app, uid, chars=9000)
    resp = _generate(paid_client, deck_id)
    assert resp.status_code == 302 and "/status" in resp.headers["Location"]
    assert _usage(paid_app, uid) == [3]
    with paid_app.app_context():
        assert billing.allowance(_db.session.get(User, uid)).remaining == 17


def test_over_quota_run_is_refused_and_settings_kept(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    deck_id = _deck(paid_app, uid, chars=21 * 4000)
    resp = paid_client.post(f"/decks/{deck_id}/preview", data={"focus": "enzymes", "target_cards": "auto"},
                            follow_redirects=True)
    assert b"needs 21 pages" in resp.data
    with paid_app.app_context():
        deck = _db.session.get(Deck, deck_id)
        assert deck.status == "draft"
        assert deck.settings_json["focus"] == "enzymes"
    assert _usage(paid_app, uid) == []
    # The preview shows the shortfall before the user even submits.
    page = paid_client.get(f"/decks/{deck_id}/preview")
    assert b"Not enough pages left" in page.data and b"disabled" in page.data


def test_reruns_of_a_deck_are_free_then_metered_again(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    deck_id = _deck(paid_app, uid, chars=4000)
    for _ in range(billing.FREE_RERUNS + 2):
        _generate(paid_client, deck_id)
    assert _usage(paid_app, uid) == [1] + [0] * billing.FREE_RERUNS + [1]


def test_replan_uses_a_free_rerun(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    deck_id = _deck(paid_app, uid, chars=4000)
    _generate(paid_client, deck_id)
    paid_client.post(f"/decks/{deck_id}/plan", data={"action": "replan"})
    assert _usage(paid_app, uid) == [1, 0]


def test_deleting_a_deck_does_not_refund_pages(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    deck_id = _deck(paid_app, uid, chars=8000)
    _generate(paid_client, deck_id)
    paid_client.post(f"/decks/{deck_id}/delete")
    with paid_app.app_context():
        record = UsageRecord.query.filter_by(user_id=uid).one()
        assert record.deck_id is None and record.deck_title == "Chapter"
        assert billing.allowance(_db.session.get(User, uid)).used == 2


def test_last_months_usage_does_not_count(paid_app, paid_client):
    register(paid_client)
    uid = _user(paid_app)
    with paid_app.app_context():
        start, _ = billing.period_bounds()
        _db.session.add(UsageRecord(user_id=uid, pages=20, chars=80000, created_at=start - timedelta(days=1)))
        _db.session.commit()
        assert billing.allowance(_db.session.get(User, uid)).used == 0


def test_quota_not_enforced_when_billing_is_off(app, client, monkeypatch):
    monkeypatch.setattr(main_routes, "dispatch_generation", lambda *a, **k: None)
    register(client)
    uid = _user(app)
    deck_id = _deck(app, uid, chars=500 * 4000)
    resp = _generate(client, deck_id)
    assert "/status" in resp.headers["Location"]
    assert _usage(app, uid) == [500]


def test_new_deck_is_trimmed_to_the_plan_deck_size(paid_app, paid_client):
    register(paid_client)
    paid_client.post("/decks/new", data={"title": "Long", "source_type": "text", "text_input": "y" * 100000})
    with paid_app.app_context():
        deck = Deck.query.filter_by(title="Long").one()
        assert len(deck.source_text) == billing.PLANS["free"].max_deck_chars


# ------------------------------------------------------------------ Stripe
def test_webhook_rejects_bad_signatures(paid_client):
    payload = json.dumps({"type": "customer.subscription.updated", "data": {"object": {}}})
    resp = paid_client.post("/billing/webhook", data=payload, headers=_signed(payload, secret="whsec_wrong"))
    assert resp.status_code == 400


def test_checkout_completion_activates_the_plan(paid_app, paid_client, monkeypatch):
    register(paid_client)
    uid = _user(paid_app)
    monkeypatch.setattr(billing, "retrieve_subscription", lambda sub_id: _subscription(sub_id, plan="pro", interval="year"))
    resp = _post_event(paid_client, "checkout.session.completed", {
        "id": "cs_1", "mode": "subscription", "subscription": "sub_1", "customer": "cus_1",
        "client_reference_id": str(uid),
    })
    assert resp.status_code == 200
    with paid_app.app_context():
        user = _db.session.get(User, uid)
        assert (user.plan, user.subscription_status, user.billing_interval) == ("pro", "active", "year")
        assert user.stripe_customer_id == "cus_1" and user.current_period_end > datetime.now()
        assert billing.allowance(user).limit == 250
        # Pro raises the per-deck size too.
        assert billing.deck_char_limit(user) == 60 * billing.CHARS_PER_PAGE


def test_subscription_events_follow_stripe_state(paid_app, paid_client, monkeypatch):
    register(paid_client)
    uid = _user(paid_app)
    with paid_app.app_context():
        _db.session.get(User, uid).stripe_customer_id = "cus_1"
        _db.session.commit()
    state = {"sub": _subscription(plan="max")}
    monkeypatch.setattr(billing, "retrieve_subscription", lambda sub_id: state["sub"])

    _post_event(paid_client, "customer.subscription.created", {"id": "sub_1", "customer": "cus_1"})
    state["sub"] = _subscription(plan="max", status="past_due", cancel_at_period_end=True)
    _post_event(paid_client, "customer.subscription.updated", {"id": "sub_1", "customer": "cus_1"})
    with paid_app.app_context():
        user = _db.session.get(User, uid)
        assert billing.plan_for(user).key == "max" and user.cancel_at_period_end

    # A late deletion of some older subscription must not cancel the live one.
    _post_event(paid_client, "customer.subscription.deleted", _subscription("sub_old", status="canceled"))
    with paid_app.app_context():
        assert billing.plan_for(_db.session.get(User, uid)).key == "max"

    _post_event(paid_client, "customer.subscription.deleted", _subscription("sub_1", status="canceled"))
    with paid_app.app_context():
        user = _db.session.get(User, uid)
        assert billing.plan_for(user).key == "free" and user.stripe_subscription_id is None


def test_checkout_redirects_to_stripe(paid_client, monkeypatch):
    register(paid_client)
    calls = {}

    def fake_checkout(user, plan, interval, success_url, cancel_url):
        calls.update(plan=plan.key, interval=interval, success_url=success_url)
        return "https://checkout.stripe.com/c/pay/cs_test_1"

    monkeypatch.setattr(billing_routes, "checkout_url", fake_checkout)
    resp = paid_client.post("/billing/checkout", data={"plan": "max", "interval": "year"})
    assert resp.status_code == 303 and resp.headers["Location"].startswith("https://checkout.stripe.com/")
    assert calls["plan"] == "max" and calls["interval"] == "year"
    assert "session_id={CHECKOUT_SESSION_ID}" in calls["success_url"]


def test_subscribers_switch_plans_in_the_portal(paid_app, paid_client, monkeypatch):
    register(paid_client)
    uid = _user(paid_app)
    with paid_app.app_context():
        user = _db.session.get(User, uid)
        user.plan, user.subscription_status, user.stripe_subscription_id = "pro", "active", "sub_1"
        user.stripe_customer_id = "cus_1"
        _db.session.commit()
    seen = {}
    monkeypatch.setattr(billing_routes, "checkout_url", lambda *a, **k: pytest.fail("opened a second subscription"))
    monkeypatch.setattr(billing_routes, "portal_url",
                        lambda user, return_url, switch_to=None: seen.setdefault("to", switch_to) and "https://billing.stripe.com/p/1")
    resp = paid_client.post("/billing/checkout", data={"plan": "max", "interval": "month"})
    assert resp.headers["Location"] == "https://billing.stripe.com/p/1"
    assert seen["to"][0].key == "max"


def test_checkout_refuses_free_and_unknown_plans(paid_client):
    register(paid_client)
    for plan in ("free", "platinum"):
        resp = paid_client.post("/billing/checkout", data={"plan": plan})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/pricing")
