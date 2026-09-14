import re

from flask import Blueprint, redirect, render_template, request, url_for, flash
from flask_login import login_required, login_user, logout_user, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from ..extensions import db
from ..models import Card, Deck, User

bp = Blueprint("auth", __name__, url_prefix="/auth")


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
    values = {
        "display_name": current_user.display_name or "",
        "bio": current_user.bio or "",
        "avatar_color": current_user.profile_color,
        "email": current_user.email,
    }
    if request.method == "POST":
        if section == "profile":
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
                       "password": "Your password is updated."}[section], "success")
                return redirect(url_for("auth.profile", _anchor=section), code=303)

    deck_count = Deck.query.filter_by(user_id=current_user.id).count()
    card_count = Card.query.join(Deck).filter(Deck.user_id == current_user.id, Card.status != "deleted").count()
    return render_template("profile.html", values=values, errors=errors, active_section=section,
                           avatar_colors=User.AVATAR_COLORS, deck_count=deck_count,
                           card_count=card_count), 422 if errors else 200
