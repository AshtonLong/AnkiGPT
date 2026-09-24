"""Create AnkiSpark's Stripe products, prices and Customer Portal configuration.

Idempotent: prices are found by lookup key (ankigpt_pro_monthly, ...) and only created
when missing, so it is safe to rerun. Run it once per Stripe mode (test, then live):

    python -m scripts.stripe_setup
    python -m scripts.stripe_setup --webhook-url https://your.domain/billing/webhook

It reads STRIPE_SECRET_KEY from the environment or .env and prints the values to add to
.env (STRIPE_PORTAL_CONFIGURATION and, with --webhook-url, STRIPE_WEBHOOK_SECRET).
Changing a plan's price in app/services/billing.py? Rerun with --reprice to move the
lookup key onto a new price; existing subscribers stay on their old price until changed.
"""

import argparse
import os
import sys

import stripe
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.billing import INTERVALS, PLANS  # noqa: E402

WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
]


def ensure_product(client, plan):
    # Products are matched by metadata (kept as ankigpt_plan so existing products are
    # found); name and description are refreshed so a rebrand reaches checkout and receipts.
    details = {
        "name": f"AnkiSpark {plan.name}",
        "description": f"{plan.pages_per_month} pages a month, decks up to {plan.pages_per_deck} pages.",
    }
    found = client.v1.products.search({"query": f"metadata['ankigpt_plan']:'{plan.key}'"})
    if found.data:
        product = found.data[0]
        if product.name != details["name"] or product.description != details["description"]:
            product = client.v1.products.update(product.id, details)
            print(f"  ~ renamed {product.id} to {details['name']}")
        return product
    return client.v1.products.create({**details, "metadata": {"ankigpt_plan": plan.key}})


def ensure_price(client, plan, interval, product, reprice):
    key = plan.lookup_key(interval)
    amount = plan.price_cents(interval)
    existing = client.v1.prices.list({"lookup_keys": [key], "active": True, "limit": 1}).data
    if existing and (existing[0].unit_amount == amount or not reprice):
        if existing[0].unit_amount != amount:
            print(f"  ! {key} is {existing[0].unit_amount} cents in Stripe but {amount} in code; rerun with --reprice")
        return existing[0]
    price = client.v1.prices.create({
        "product": product.id,
        "currency": "usd",
        "unit_amount": amount,
        "recurring": {"interval": interval},
        "lookup_key": key,
        "transfer_lookup_key": True,
        "tax_behavior": "exclusive",
        "metadata": {"ankigpt_plan": plan.key},
    })
    print(f"  + created {key}: ${amount / 100:.2f}/{interval}")
    return price


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webhook-url", help="Public URL of /billing/webhook to register")
    parser.add_argument("--reprice", action="store_true", help="Replace prices whose amount changed in code")
    args = parser.parse_args()

    load_dotenv()
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("Set STRIPE_SECRET_KEY (sk_test_... or sk_live_...) first.")
    print(f"Stripe {'LIVE' if key.startswith(('sk_live', 'rk_live')) else 'test'} mode")
    client = stripe.StripeClient(key)

    portal_products = []
    for plan in PLANS.values():
        if not plan.paid:
            continue
        product = ensure_product(client, plan)
        prices = [ensure_price(client, plan, interval, product, args.reprice) for interval in INTERVALS]
        portal_products.append({"product": product.id, "prices": [p.id for p in prices]})
        print(f"  {plan.name}: {product.id} ({', '.join(p.id for p in prices)})")

    portal_params = {
        "business_profile": {"headline": "Manage your AnkiSpark plan"},
        "features": {
            "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "name"]},
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "products": portal_products,
                "proration_behavior": "create_prorations",
            },
        },
    }
    existing_portal = os.getenv("STRIPE_PORTAL_CONFIGURATION", "")
    if existing_portal:
        portal = client.v1.billing_portal.configurations.update(existing_portal, portal_params)
    else:
        portal = client.v1.billing_portal.configurations.create(portal_params)
    print("\nAdd to .env:")
    print(f"STRIPE_PORTAL_CONFIGURATION={portal.id}")

    if args.webhook_url:
        endpoint = client.v1.webhook_endpoints.create({"url": args.webhook_url, "enabled_events": WEBHOOK_EVENTS})
        print(f"STRIPE_WEBHOOK_SECRET={endpoint.secret}")
    else:
        print("# Local testing: stripe listen --forward-to localhost:5000/billing/webhook")
        print("# then set STRIPE_WEBHOOK_SECRET to the whsec_... it prints.")
    print("BILLING_ENABLED=true")


if __name__ == "__main__":
    main()
