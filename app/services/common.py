"""Audit log and email outbox, shared by every module."""
import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from app.config import get_settings
from app.utils import new_id, utcnow

log = logging.getLogger("suppai.vendor")


async def audit(db: Any, admin: dict, action: str, target_type: str, target_id: str, **details: Any) -> None:
    """Every admin action is logged with who and when."""
    await db.audit_logs.insert_one({
        "_id": new_id("aud"),
        "admin_id": admin["_id"],
        "admin_email": admin.get("email"),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
        "at": utcnow(),
    })


def _smtp_send(msg: EmailMessage) -> None:
    s = get_settings()
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def send_email(db: Any, to: str, subject: str, body: str, kind: str) -> None:
    """Records every mail in `outbox`; sends over SMTP when configured. Never raises."""
    s = get_settings()
    record = {"_id": new_id("mail"), "to": to, "subject": subject, "body": body,
              "kind": kind, "created_at": utcnow(), "status": "logged"}
    if s.smtp_host:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
        msg.set_content(body)
        try:
            await asyncio.to_thread(_smtp_send, msg)
            record["status"] = "sent"
        except Exception as e:  # mail failure must not fail the business action
            log.exception("Email to %s failed", to)
            record.update(status="failed", error=str(e))
    else:
        log.info("Email (not sent, no SMTP) to=%s subject=%s", to, subject)
    await db.outbox.insert_one(record)
