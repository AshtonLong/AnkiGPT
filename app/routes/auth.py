import hashlib
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, redirect, render_template, request, url_for, flash
from flask_login import login_required, login_user, logout_user, current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from ..desktop import is_desktop
from ..extensions import db
from ..models import Card, Deck, User
from ..services.credentials import key_problem, openrouter_key_for, set_user_key, user_key
from ..services.pipeline.efforts import EFFORT_LABELS, effort_groups, efforts_from_form, set_user_efforts
from ..services.mailer import send_mail

RESET_MAX_AGE = 3600  # seconds a reset link stays valid
RESET_THROTTLE = timedelta(minutes=2)

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.app_context_processor
def api_key_context():
    def needs_api_key():
        """True while a signed-in user has no OpenRouter key to generate with."""
        return current_user.is_authenticated and not openrouter_key_for(current_user)

    return {"needs_api_key": needs_api_key}


def _login_destination():
    # Only accept the account destination we offer, never arbitrary redirect URLs.
    if request.args.get("next") == url_for("auth.profile"):
        return url_for("auth.profile")
    return url_for("main.decks")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(_login_destination())
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required", "error")
            return render_template("auth_signup.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("auth_signup.html")
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "error")
            return render_template("auth_signup.html")
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(_login_destination())
    return render_template("auth_signup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_login_destination())
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid credentials", "error")
            return render_template("auth_login.html")
        login_user(user)
        return redirect(_login_destination())
    return render_template("auth_login.html")


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def _password_fingerprint(user):
    # Part of the token, so a link stops working once the password it resets has changed.
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def make_reset_token(user):
    return _reset_serializer().dumps({"uid": user.id, "pw": _password_fingerprint(user)})


def user_for_reset_token(token):
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, data.get("uid")) if isinstance(data, dict) else None
    if user is None or data.get("pw") != _password_fingerprint(user):
        return None
    return user


@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("auth.profile", _anchor="password"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter(func.lower(User.email) == email).first() if email else None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if user and not (user.reset_requested_at and now - user.reset_requested_at < RESET_THROTTLE):
            user.reset_requested_at = now
            db.session.commit()
            link = url_for("auth.reset_password", token=make_reset_token(user), _external=True)
            send_mail(
                user.email,
                "Reset your AnkiGPT password",
                render_template("email/password_reset.txt", link=link, user=user),
                render_template("email/password_reset.html", link=link, user=user),
            )
        # Same answer whether or not the account exists, so the form can't be used to
        # find out who has an account.
        return render_template("auth_forgot.html", sent_to=email)
    return render_template("auth_forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = user_for_reset_token(token)
    if user is None:
        return render_template("auth_reset.html", invalid=True), 400
    errors = {}
    if request.method == "POST":
        password = request.form.get("new_password", "")
        if len(password) < 8 or len(password) > 128:
            errors["new_password"] = "Use between 8 and 128 characters."
        elif password != request.form.get("confirm_password", ""):
            errors["confirm_password"] = "The passwords don't match."
        if not errors:
            user.set_password(password)
            user.reset_requested_at = None
            db.session.commit()
            logout_user()
            login_user(user)
            flash("Your password is updated. You're signed in.", "success")
            return redirect(url_for("main.decks"))
    return render_template("auth_reset.html", errors=errors), 422 if errors else 200


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.index"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    errors = {}
    section = request.form.get("section", "") if request.method == "POST" else ""
    key_notice = "Your OpenRouter key is saved. You're ready to generate."
    effort_notice = "Your effort settings are saved. They apply from your next run."
    # Desktop has no account to edit: this URL is its Settings page, with the key and the
    # advanced settings as its only forms.
    desktop = is_desktop()
    values = {
        "display_name": current_user.display_name or "",
        "bio": current_user.bio or "",
        "avatar_color": current_user.profile_color,
        "email": current_user.email,
    }
    if request.method == "POST":
        if desktop and section not in ("api-key", "advanced"):
            errors["form"] = "Choose a setting to update."
        elif section == "profile":
            values.update({key: request.form.get(key, "").strip()
                           for key in ("display_name", "bio", "avatar_color")})
            if len(values["display_name"]) > 80:
                errors["display_name"] = "Keep your display name to 80 characters or fewer."
            if len(values["bio"]) > 280:
                errors["bio"] = "Keep your bio to 280 characters or fewer."
            if values["avatar_color"] not in User.AVATAR_COLORS:
                errors["avatar_color"] = "Choose one of the available avatar colors."
            if not errors:
                current_user.display_name = values["display_name"] or None
                current_user.bio = values["bio"] or None
                current_user.avatar_color = values["avatar_color"]
        elif section == "email":
            values["email"] = request.form.get("email", "").strip().lower()
            if len(values["email"]) > 255 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
                errors["email"] = "Enter a valid email address."
            elif User.query.filter(func.lower(User.email) == values["email"], User.id != current_user.id).first():
                errors["email"] = "That email address is already in use."
            if not current_user.check_password(request.form.get("email_password", "")):
                errors["email_password"] = "Your current password doesn't match. Try again."
            if not errors:
                current_user.email = values["email"]
        elif section == "password":
            password = request.form.get("new_password", "")
            if not current_user.check_password(request.form.get("current_password", "")):
                errors["current_password"] = "Your current password doesn't match. Try again."
            if len(password) < 8 or len(password) > 128:
                errors["new_password"] = "Use between 8 and 128 characters."
            elif current_user.check_password(password):
                errors["new_password"] = "Choose a password different from your current one."
            if password != request.form.get("confirm_password", ""):
                errors["confirm_password"] = "The new passwords don't match."
            if not errors:
                current_user.set_password(password)
        elif section == "api-key":
            if request.form.get("action") == "remove":
                set_user_key(current_user, "")
                key_notice = "Your OpenRouter key is removed."
            else:
                key = request.form.get("openrouter_api_key", "").strip()
                problem = key_problem(key)
                if problem:
                    errors["openrouter_api_key"] = problem
                else:
                    set_user_key(current_user, key)
        elif section == "advanced":
            if request.form.get("action") == "reset":
                set_user_efforts(current_user, {})
                effort_notice = "Every agent is back on its default effort."
            else:
                efforts = efforts_from_form(request.form)
                if efforts is None:
                    errors["form"] = "Set each agent's effort with its slider, then save again."
                else:
                    set_user_efforts(current_user, efforts)
        else:
            errors["form"] = "Choose a profile setting to update."

        if not errors:
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["email"] = "That email address is already in use."
            else:
                flash({"profile": "Your profile is saved.", "email": "Your sign-in email is updated.",
                       "password": "Your password is updated.", "api-key": key_notice,
                       "advanced": effort_notice}[section], "success")
                return redirect(url_for("auth.profile", _anchor=section), code=303)

    deck_count = Deck.query.filter_by(user_id=current_user.id).count()
    card_count = Card.query.join(Deck).filter(Deck.user_id == current_user.id, Card.status != "deleted").count()
    # The key itself never goes back to the browser, only whether one is saved and its last characters.
    key_saved = bool(user_key(current_user))
    api_key = {
        "saved": key_saved,
        # Encrypted with a SECRET_KEY the server no longer uses; it has to be entered again.
        "unreadable": bool(current_user.openrouter_key_encrypted) and not key_saved,
        "hint": current_user.openrouter_key_hint or "",
        "server_fallback": bool(current_app.config.get("OPENROUTER_API_KEY")),
    }
    advanced = {"effort_groups": effort_groups(current_user, current_app.config),
                "effort_labels": list(EFFORT_LABELS.values())}
    if desktop:
        return render_template("settings.html", errors=errors, deck_count=deck_count, card_count=card_count,
                               api_key=api_key, data_dir=current_app.config["DESKTOP_DATA_DIR"],
                               app_version=current_app.config["APP_VERSION"], **advanced), 422 if errors else 200
    return render_template("profile.html", values=values, errors=errors, active_section=section,
                           avatar_colors=User.AVATAR_COLORS, deck_count=deck_count,
                           card_count=card_count, api_key=api_key, **advanced), 422 if errors else 200
