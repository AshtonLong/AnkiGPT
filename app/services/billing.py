"""Plans, page metering and Stripe subscriptions.

Usage is metered in *pages*: 4,000 characters of extracted source text, roughly one
dense textbook page. Generation cost scales with source length, so pages track what a
deck actually costs to build; slide decks and sparse PDFs use less than their page count.

Stripe is the source of truth for subscriptions. Checkout and the Customer Portal
handle every payment screen; the webhook mirrors the subscription onto the User row,
and `plan_for` only honours a paid plan while that subscription is live.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import stripe
from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import UsageRecord, User

logger = logging.getLogger(__name__)

CHARS_PER_PAGE = 4000
# A deck's first run is metered. The next reruns (re-plan, retry after a failure) are
# free; after that each run is metered again so endless re-planning stays bounded.
FREE_RERUNS = 2
# past_due keeps access while Stripe retries the card; Stripe cancels if it never clears.
ACTIVE_STATUSES = frozenset({"active", "trialing", "past_due"})
INTERVALS = ("month", "year")


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    tagline: str
    monthly_cents: int
    yearly_cents: int
    pages_per_month: int
    pages_per_deck: int
    features: tuple

    @property
    def paid(self):
        return self.monthly_cents > 0

    @property
    def max_deck_chars(self):
        return self.pages_per_deck * CHARS_PER_PAGE

    def price_cents(self, interval):
        return self.yearly_cents if interval == "year" else self.monthly_cents

    def lookup_key(self, interval):
        """Stripe price lookup key, e.g. ankigpt_pro_monthly. scripts/stripe_setup.py
        creates prices under these keys, so no price ids live in config."""
        return f"ankigpt_{self.key}_{interval}ly"


PLANS = {
    plan.key: plan
    for plan in (
        Plan("free", "Free", "Try it on a lecture or a chapter.", 0, 0, 20, 20,
             ("Decks up to 20 pages", "Every feature included", "No credit card needed")),
        Plan("pro", "Pro", "For a full course load, all term.", 800, 8000, 250, 60,
             ("Decks up to 60 pages", "About 10 textbook chapters a month", "Cancel anytime")),
        Plan("max", "Max", "For exam season and dense textbooks.", 2000, 20000, 700, 100,
             ("Decks up to 100 pages", "About 28 textbook chapters a month", "Cancel anytime")),
    )
}
# Included on every plan; plans differ only in how much source they can turn into cards.
SHARED_FEATURES = (
    "Planner you can review before writing",
    "Figure reading with vision",
    "Card Coach from your Anki reviews",
    "AI fixes for individual cards",
    "Native .apkg export",
)


class BillingError(RuntimeError):
    """Billing is misconfigured or Stripe refused a request; safe to show the user."""


class QuotaExceeded(Exception):
    def __init__(self, needed, allowance):
        super().__init__(f"Needs {needed} pages, {allowance.remaining} left")
        self.needed = needed
        self.allowance = allowance


def billing_enabled():
    return bool(current_app.config.get("BILLING_ENABLED"))


def pages_for(chars):
    return max(1, math.ceil((chars or 0) / CHARS_PER_PAGE))


def plan_for(user):
    if user is not None and user.plan in PLANS and user.subscription_status in ACTIVE_STATUSES:
        return PLANS[user.plan]
    return PLANS["free"]


def has_live_subscription(user):
    return bool(user.stripe_subscription_id) and user.subscription_status in ACTIVE_STATUSES


def period_bounds(now=None):
    """The current allowance window: the UTC calendar month, as naive UTC datetimes to
    match how DateTime columns come back from SQLite and Postgres."""
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    return start, end


@dataclass
class Allowance:
    plan: Plan
    used: int
    reset_at: datetime

    @property
    def limit(self):
        return self.plan.pages_per_month

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    @property
    def pct(self):
        return min(100, round(100 * self.used / self.limit)) if self.limit else 100

    @property
    def reset_label(self):
        return f"{self.reset_at:%b} {self.reset_at.day}"


def allowance(user, now=None):
    start, end = period_bounds(now)
    used = (
        db.session.query(func.coalesce(func.sum(UsageRecord.pages), 0))
        .filter(UsageRecord.user_id == user.id, UsageRecord.created_at >= start)
        .scalar()
    )
    return Allowance(plan=plan_for(user), used=int(used or 0), reset_at=end)


def run_charge(deck):
    """Pages the next generation run of `deck` will cost (0 for an included rerun)."""
    runs = UsageRecord.query.filter_by(deck_id=deck.id).count()
    if 0 < runs <= FREE_RERUNS:
        return 0
    return pages_for(len(deck.source_text or ""))


def meter_generation(user, deck):
    """Record a generation run against the monthly allowance, or raise QuotaExceeded.

    Usage is always recorded; the allowance is only enforced with billing enabled. The
    record joins the caller's transaction, so it is committed with the deck's status.
    """
    charge = run_charge(deck)
    if charge and billing_enabled():
        current = allowance(user)
        if charge > current.remaining:
            raise QuotaExceeded(charge, current)
    db.session.add(UsageRecord(
        user_id=user.id, deck_id=deck.id, deck_title=(deck.title or "")[:200],
        pages=charge, chars=len(deck.source_text or ""),
    ))
    return charge


def deck_char_limit(user):
    """Longest source a new deck may keep: the plan's per-deck size, under the
    server-wide MAX_SOURCE_CHARS guard. 0 means unlimited."""
    global_cap = current_app.config.get("MAX_SOURCE_CHARS") or 0
    if not billing_enabled():
        return global_cap
    plan_cap = plan_for(user).max_deck_chars
    return min(plan_cap, global_cap) if global_cap else plan_cap


def recent_usage(user, limit=8):
    return (
        UsageRecord.query.filter_by(user_id=user.id)
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
        .limit(limit)
        .all()
    )


# ------------------------------------------------------------------------ Stripe
def _client():
    key = current_app.config.get("STRIPE_SECRET_KEY")
    if not key:
        raise BillingError("Payments aren't configured on this server yet.")
    return stripe.StripeClient(key)


_price_ids = {}


def price_id(plan, interval):
    key = plan.lookup_key(interval)
    if key not in _price_ids:
        prices = _client().v1.prices.list({"lookup_keys": [key], "active": True, "limit": 1})
        if not prices.data:
            logger.error("No active Stripe price with lookup key %s; run scripts/stripe_setup.py", key)
            raise BillingError("That plan isn't available right now.")
        _price_ids[key] = prices.data[0].id
    return _price_ids[key]


def retrieve_subscription(subscription_id):
    return _client().v1.subscriptions.retrieve(subscription_id).to_dict()


def retrieve_checkout_session(session_id):
    return _client().v1.checkout.sessions.retrieve(session_id).to_dict()


def ensure_customer(user):
    if not user.stripe_customer_id:
        params = {"email": user.email, "metadata": {"user_id": str(user.id)}}
        if user.display_name:
            params["name"] = user.display_name
        customer = _client().v1.customers.create(params)
        user.stripe_customer_id = customer.id
        db.session.commit()
    return user.stripe_customer_id


def checkout_url(user, plan, interval, success_url, cancel_url):
    params = {
        "mode": "subscription",
        "customer": ensure_customer(user),
        "client_reference_id": str(user.id),
        "line_items": [{"price": price_id(plan, interval), "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
        "subscription_data": {"metadata": {"user_id": str(user.id)}},
    }
    if current_app.config.get("STRIPE_AUTOMATIC_TAX"):
        params["automatic_tax"] = {"enabled": True}
        params["customer_update"] = {"address": "auto", "name": "auto"}
    return _client().v1.checkout.sessions.create(params).url


def portal_url(user, return_url, switch_to=None):
    """Customer Portal session. With `switch_to=(plan, interval)` and a live subscription,
    opens straight onto Stripe's confirm-the-change screen with proration shown."""
    params = {"customer": ensure_customer(user), "return_url": return_url}
    configuration = current_app.config.get("STRIPE_PORTAL_CONFIGURATION")
    if configuration:
        params["configuration"] = configuration
    if switch_to and has_live_subscription(user):
        plan, interval = switch_to
        subscription = retrieve_subscription(user.stripe_subscription_id)
        items = (subscription.get("items") or {}).get("data") or []
        if items:
            params["flow_data"] = {
                "type": "subscription_update_confirm",
                "subscription_update_confirm": {
                    "subscription": user.stripe_subscription_id,
                    "items": [{"id": items[0]["id"], "price": price_id(plan, interval), "quantity": 1}],
                },
                "after_completion": {"type": "redirect", "redirect": {"return_url": return_url}},
            }
    return _client().v1.billing_portal.sessions.create(params).url


def _plan_from_price(price):
    lookup = price.get("lookup_key") or ""
    for plan in PLANS.values():
        for interval in INTERVALS:
            if plan.paid and plan.lookup_key(interval) == lookup:
                return plan.key, interval
    return None, None


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None) if timestamp else None


def apply_subscription(user, subscription):
    """Mirror a Stripe subscription (as a plain dict) onto the user."""
    sub_id = subscription.get("id")
    status = subscription.get("status")
    items = (subscription.get("items") or {}).get("data") or []
    item = items[0] if items else {}
    plan_key, interval = _plan_from_price(item.get("price") or {})
    ended = status in ("canceled", "incomplete_expired") or not plan_key
    if ended:
        # A late event for an old subscription must not wipe out a newer one.
        if user.stripe_subscription_id in (None, sub_id):
            user.plan = None
            user.stripe_subscription_id = None
            user.subscription_status = status
            user.billing_interval = None
            user.current_period_end = None
            user.cancel_at_period_end = False
        return
    user.plan = plan_key
    user.stripe_subscription_id = sub_id
    user.subscription_status = status
    user.billing_interval = interval
    # Newer API versions carry the billing period on the subscription item.
    user.current_period_end = _utc(item.get("current_period_end") or subscription.get("current_period_end"))
    user.cancel_at_period_end = bool(subscription.get("cancel_at_period_end") or subscription.get("cancel_at"))


def _user_for(customer_id, user_id=None):
    user = User.query.filter_by(stripe_customer_id=customer_id).first() if customer_id else None
    if user is None and user_id and str(user_id).isdigit():
        user = db.session.get(User, int(user_id))
        if user is not None and user.stripe_customer_id not in (None, customer_id):
            return None
        if user is not None:
            user.stripe_customer_id = customer_id
    return user


def handle_event(event):
    """Apply one verified webhook event. Subscription events re-read the subscription
    from Stripe, so out-of-order delivery always lands on the current state."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return False
        user = _user_for(obj.get("customer"), obj.get("client_reference_id"))
        if user is None:
            logger.warning("Checkout session %s matches no user", obj.get("id"))
            return False
        apply_subscription(user, retrieve_subscription(obj["subscription"]))
    elif kind.startswith("customer.subscription."):
        user = _user_for(obj.get("customer"), (obj.get("metadata") or {}).get("user_id"))
        if user is None:
            logger.warning("Subscription %s matches no user", obj.get("id"))
            return False
        fresh = obj if kind == "customer.subscription.deleted" else retrieve_subscription(obj["id"])
        apply_subscription(user, fresh)
    else:
        return False
    db.session.commit()
    return True


def sync_checkout(user, session_id):
    """Apply a just-finished checkout on the success redirect, so the new plan shows
    immediately even if the webhook hasn't arrived yet."""
    session = retrieve_checkout_session(session_id)
    if session.get("client_reference_id") != str(user.id) or not session.get("subscription"):
        return False
    subscription = session["subscription"]
    if isinstance(subscription, str):
        subscription = retrieve_subscription(subscription)
    if not user.stripe_customer_id:
        user.stripe_customer_id = session.get("customer")
    apply_subscription(user, subscription)
    db.session.commit()
    return True
