import json

import stripe
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import csrf, db
from ..services.billing import (
    CHARS_PER_PAGE,
    FREE_RERUNS,
    INTERVALS,
    PLANS,
    SHARED_FEATURES,
    BillingError,
    allowance,
    billing_enabled,
    checkout_url,
    handle_event,
    has_live_subscription,
    plan_for,
    portal_url,
    recent_usage,
    sync_checkout,
)

bp = Blueprint("billing", __name__)


@bp.app_context_processor
def billing_context():
    def current_allowance():
        return allowance(current_user) if current_user.is_authenticated else None

    return {"billing_enabled": billing_enabled(), "billing_allowance": current_allowance}


def _plan_context():
    return {
        "plans": list(PLANS.values()),
        "shared_features": SHARED_FEATURES,
        "chars_per_page": CHARS_PER_PAGE,
        "free_reruns": FREE_RERUNS,
        "current_plan": plan_for(current_user) if current_user.is_authenticated else None,
        "subscribed": current_user.is_authenticated and has_live_subscription(current_user),
    }


@bp.route("/pricing")
def pricing():
    return render_template("pricing.html", **_plan_context())


@bp.route("/billing")
@login_required
def account():
    return render_template(
        "billing.html", allowance=allowance(current_user), usage=recent_usage(current_user),
        **_plan_context(),
    )


def _stripe_failure(message="We couldn't reach our payment provider. Try again in a moment."):
    current_app.logger.exception("Stripe request failed")
    flash(message, "error")
    return redirect(url_for("billing.account"))


@bp.route("/billing/checkout", methods=["POST"])
@login_required
def checkout():
    plan = PLANS.get(request.form.get("plan", ""))
    interval = request.form.get("interval", "month")
    if not plan or not plan.paid or interval not in INTERVALS:
        flash("Choose a plan to continue.", "error")
        return redirect(url_for("billing.pricing"))
    if not billing_enabled():
        flash("Paid plans aren't available on this server.", "info")
        return redirect(url_for("billing.pricing"))
    return_url = url_for("billing.account", _external=True)
    try:
        if has_live_subscription(current_user):
            # Already subscribed: switch plans in the portal so Stripe prorates the
            # change on the existing subscription instead of opening a second one.
            url = portal_url(current_user, return_url, switch_to=(plan, interval))
        else:
            url = checkout_url(
                current_user, plan, interval,
                success_url=url_for("billing.success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=url_for("billing.pricing", _external=True),
            )
    except BillingError as exc:
        current_app.logger.warning("Checkout unavailable: %s", exc)
        flash(str(exc), "error")
        return redirect(url_for("billing.pricing"))
    except stripe.StripeError:
        return _stripe_failure()
    return redirect(url, code=303)


@bp.route("/billing/portal", methods=["POST"])
@login_required
def portal():
    if not billing_enabled() or not current_user.stripe_customer_id:
        return redirect(url_for("billing.account"))
    try:
        url = portal_url(current_user, url_for("billing.account", _external=True))
    except (BillingError, stripe.StripeError):
        return _stripe_failure()
    return redirect(url, code=303)


@bp.route("/billing/success")
@login_required
def success():
    session_id = request.args.get("session_id", "")
    if billing_enabled() and session_id.startswith("cs_"):
        try:
            synced = sync_checkout(current_user, session_id)
        except (BillingError, stripe.StripeError):
            current_app.logger.exception("Could not sync checkout %s; waiting for the webhook", session_id)
            db.session.rollback()
            synced = False
        if synced:
            flash(f"You're on {plan_for(current_user).name}. Your new pages are ready to use.", "success")
        else:
            flash("Payment received. Your plan will update in a few seconds.", "info")
    return redirect(url_for("billing.account"))


@bp.route("/billing/webhook", methods=["POST"])
@csrf.exempt
def webhook():
    secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    if not billing_enabled() or not secret:
        return {"error": "billing is not configured"}, 404
    payload = request.get_data()
    try:
        stripe.WebhookSignature.verify_header(payload, request.headers.get("Stripe-Signature"), secret)
        event = json.loads(payload)
    except (stripe.SignatureVerificationError, ValueError):
        return {"error": "invalid signature"}, 400
    try:
        handle_event(event)
    except stripe.StripeError:
        # A non-2xx response makes Stripe retry the delivery later.
        current_app.logger.exception("Webhook %s failed", event.get("id"))
        db.session.rollback()
        return {"error": "retry"}, 502
    return {"received": True}
