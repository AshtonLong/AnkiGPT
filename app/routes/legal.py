from flask import Blueprint, current_app, render_template

bp = Blueprint("legal", __name__)

# Bump when the text of any legal page changes.
LEGAL_UPDATED = "October 5, 2026"


@bp.app_context_processor
def legal_context():
    config = current_app.config
    return {"legal": {
        "name": config.get("LEGAL_NAME") or "AnkiGPT",
        "email": config.get("SUPPORT_EMAIL") or "",
        "jurisdiction": config.get("LEGAL_JURISDICTION") or "Canada",
        "updated": LEGAL_UPDATED,
    }}


@bp.route("/terms")
def terms():
    return render_template("legal/terms.html")


@bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")
