"""Minimal transactional email over SMTP (password resets).

Provider-neutral: Resend, Postmark, Brevo and Gmail all speak SMTP. Without
MAIL_SMTP_HOST the message is logged instead, so local development still works.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger(__name__)


def mail_configured():
    return bool(current_app.config.get("MAIL_SMTP_HOST") and current_app.config.get("MAIL_FROM"))


def send_mail(to, subject, text, html=None):
    """Send one message. Returns True if handed to the SMTP server, False if it was only
    logged (unconfigured) or the send failed (logged with the traceback)."""
    config = current_app.config
    if not mail_configured():
        logger.warning("Email not configured; would send to %s: %s\n%s", to, subject, text)
        return False
    message = EmailMessage()
    message["From"] = config["MAIL_FROM"]
    message["To"] = to
    message["Subject"] = subject
    if config.get("SUPPORT_EMAIL"):
        message["Reply-To"] = config["SUPPORT_EMAIL"]
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    host, port = config["MAIL_SMTP_HOST"], int(config.get("MAIL_SMTP_PORT") or 465)
    try:
        context = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=context)
        with server:
            if config.get("MAIL_SMTP_USERNAME"):
                server.login(config["MAIL_SMTP_USERNAME"], config.get("MAIL_SMTP_PASSWORD", ""))
            server.send_message(message)
    except (smtplib.SMTPException, OSError):
        logger.exception("Sending email to %s failed", to)
        return False
    return True
