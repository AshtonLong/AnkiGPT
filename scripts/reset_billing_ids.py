"""Clear Stripe customer/subscription ids from every user.

Run once when switching STRIPE_SECRET_KEY from test mode to live mode: test-mode ids
don't exist in live mode, so a test subscription would otherwise keep granting a paid
plan and the Customer Portal would fail to open. Usage records are kept.

    python -m scripts.reset_billing_ids          # show what would change
    python -m scripts.reset_billing_ids --yes    # apply
"""

import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="Apply the change")
    args = parser.parse_args()

    load_dotenv()
    from app import create_app
    from app.extensions import db
    from app.models import User

    app = create_app()
    with app.app_context():
        users = User.query.filter(
            (User.stripe_customer_id.isnot(None)) | (User.stripe_subscription_id.isnot(None)) | (User.plan.isnot(None))
        ).all()
        for user in users:
            print(f"  user {user.id} {user.email}: plan={user.plan} status={user.subscription_status} "
                  f"customer={user.stripe_customer_id} subscription={user.stripe_subscription_id}")
        if not users:
            print("No users have Stripe billing data.")
            return
        if not args.yes:
            print(f"\n{len(users)} user(s) would be reset to Free. Rerun with --yes to apply.")
            return
        for user in users:
            user.plan = user.stripe_customer_id = user.stripe_subscription_id = None
            user.subscription_status = user.billing_interval = user.current_period_end = None
            user.cancel_at_period_end = False
        db.session.commit()
        print(f"\nReset {len(users)} user(s) to Free.")


if __name__ == "__main__":
    main()
