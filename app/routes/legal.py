from flask import Blueprint, current_app, render_template

from ..services.billing import CHARS_PER_PAGE, FREE_RERUNS, PLANS

bp = Blueprint("legal", __name__)

# Bump when the text of any legal page changes.
LEGAL_UPDATED = "September 23, 2026"


@bp.app_context_processor
def legal_context():
    config = current_app.config
    return {"legal": {
        "name": config.get("LEGAL_NAME") or "AnkiSpark",
        "email": config.get("SUPPORT_EMAIL") or "",
        "jurisdiction": config.get("LEGAL_JURISDICTION") or "Canada",
        "updated": LEGAL_UPDATED,
    }}


def _page(template):
    return render_template(template, plans=list(PLANS.values()), chars_per_page=CHARS_PER_PAGE,
                           free_reruns=FREE_RERUNS)


@bp.route("/terms")
def terms():
    return _page("legal/terms.html")


@bp.route("/privacy")
def privacy():
    return _page("legal/privacy.html")


@bp.route("/refunds")
def refunds():
    return _page("legal/refunds.html")
