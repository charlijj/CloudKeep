"""Email notifications — GROUNDWORK, disabled by default.

With SMTP_ENABLED=false (the default) nothing is sent and no socket is opened:
the notify_* helpers only LOG what they would send. The real send path (stdlib
smtplib + optional STARTTLS) is fully written and gated behind the flag, so
turning email on later is purely a config change in .env — SMTP_ENABLED plus the
SMTP_* values — with no code change. Failures never propagate to the caller, so
email can never break the (more important) account-approval flow.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("cloudkeep.notify")


def notify_account_approved(settings, *, email: str | None, username: str,
                            first_name: str | None = None) -> bool:
    """Tell an approved user their account is ready. Returns True iff an email
    was actually sent (always False while SMTP is disabled)."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    body = (
        f"{greeting}\n\n"
        f"Your CloudKeep account '{username}' has been approved. "
        f"You can sign in at {settings.portal_url}/app/.\n\n"
        "Your administrator will share your initial password separately.\n"
    )
    return _send(settings, to=email, subject="Your CloudKeep account is ready",
                 body=body)


def _send(settings, *, to: str | None, subject: str, body: str) -> bool:
    # Inert path: with email off (or no recipient) we open no connection and
    # just record intent, so the operator sees what *would* have been sent.
    if not (settings.SMTP_ENABLED and settings.SMTP_HOST and to):
        logger.info("notify (email disabled) -> would send %r to %s",
                    subject, to or "<no address>")
        return False

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USERNAME
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as s:
            if settings.SMTP_USE_TLS:
                s.starttls()
            if settings.SMTP_USERNAME:
                s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            s.send_message(msg)
        logger.info("notify -> emailed %s: %r", to, subject)
        return True
    except Exception as exc:        # email is best-effort; never break approval
        logger.warning("notify failed for %s: %s", to, exc)
        return False
