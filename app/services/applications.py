"""Vendor application lifecycle: draft -> submitted -> reviewing / on_hold -> accepted | rejected."""
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import sess, transaction
from app.schemas.applications import STEP_MODELS, required_documents
from app.security import new_secret_token
from app.services.common import audit, send_email
from app.utils import mask, new_id, out, username_base, utcnow

# Which admin decisions are allowed from which status.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"submitted"},
    "submitted": {"reviewing", "on_hold", "rejected", "accepted"},
    "reviewing": {"on_hold", "rejected", "accepted"},
    "on_hold": {"reviewing", "rejected", "accepted"},
    "accepted": set(),
    "rejected": set(),
}

CYCLE_DAYS = {"weekly": 7, "fortnightly": 14, "monthly": 30}


def ensure_transition(app: dict, to: str) -> None:
    if to not in TRANSITIONS.get(app["status"], set()):
        raise HTTPException(409, f"Cannot move an application from '{app['status']}' to '{to}'")


def missing_for_submit(app: dict) -> list[str]:
    """Re-validates every step and lists what still blocks submission."""
    problems: list[str] = []
    steps = app.get("steps", {})
    for key, model in STEP_MODELS.items():
        if key not in steps:
            problems.append(f"Step '{key}' is not filled")
            continue
        data = dict(steps[key])
        if key == "signatory":
            continue  # stored masked; it was validated on save
        try:
            model.model_validate(data)
        except ValidationError as e:
            problems.append(f"Step '{key}': {e.errors()[0]['msg']}")
    categories = steps.get("statutory", {}).get("categories", [])
    uploaded = {d["doc_type"] for d in app.get("documents", [])}
    for doc in sorted(required_documents(categories) - uploaded):
        problems.append(f"Document '{doc}' is required")
    return problems


def admin_view(app: dict, full: bool = False) -> dict:
    d = out(app, hide=("draft_token_hash",))
    if not full:
        bank = d.get("steps", {}).get("bank")
        if bank:
            d["steps"] = {**d["steps"], "bank": {**bank, "account_number": mask(bank.get("account_number"))}}
    return d


async def set_status(db: Any, app: dict, to: str, admin: dict | None, note: str | None, session: Any = None, **extra: Any) -> None:
    ensure_transition(app, to)
    entry = {"status": to, "at": utcnow(), "by": admin["_id"] if admin else "vendor", "note": note}
    await db.vendor_applications.update_one(
        {"_id": app["_id"], "status": app["status"]},  # guards against two admins acting at once
        {"$set": {"status": to, "updated_at": entry["at"], "admin_note": note, **extra},
         "$push": {"status_history": entry}},
        **sess(session),
    )


async def _unique_username(db: Any, legal_name: str, session: Any) -> str:
    base = username_base(legal_name)
    candidate, n = f"{base}.vendor", 1
    while await db.vendor_users.find_one({"username": candidate}, **sess(session)):
        n += 1
        candidate = f"{base}{n}.vendor"
    return candidate


async def accept(db: Any, app: dict, admin: dict, commission_pct: float | None, note: str | None) -> dict:
    ensure_transition(app, "accepted")
    s = get_settings()
    steps = app["steps"]
    company, statutory, bank, commercial = steps["company"], steps["statutory"], steps["bank"], steps["commercial"]
    email = company["official_email"].lower()
    if await db.vendor_users.find_one({"email": email}):
        raise HTTPException(409, f"A vendor login already exists for {email}")

    now = utcnow()
    vendor_id, user_id = new_id("ven"), new_id("vus")
    raw_token, token_hash = new_secret_token()
    commission = commission_pct if commission_pct is not None else s.default_commission_pct

    try:
        async with transaction() as session:
            username = await _unique_username(db, company["legal_name"], session)
            await db.vendors.insert_one({
                "_id": vendor_id,
                "application_id": app["_id"],
                "reference": app["reference"],
                "legal_name": company["legal_name"],
                "display_name": company.get("trade_name") or company["legal_name"],
                "entity_type": company["entity_type"],
                "official_email": email,
                "phone": company["phone"],
                "registered_address": company["registered_address"],
                "warehouse_address": company.get("warehouse_address") or company["registered_address"],
                "categories": statutory["categories"],
                "gstin": statutory["gstin"],
                "bank": bank,
                "settlement_cycle": bank["settlement_cycle"],
                "trade_discount_pct": commercial["trade_discount_pct"],
                "delivery_days": commercial["delivery_days"],
                "return_window_days": commercial["return_window_days"],
                "commission_pct": commission,
                "status": "active",
                "next_payout_at": now + timedelta(days=CYCLE_DAYS[bank["settlement_cycle"]]),
                "created_at": now,
                "created_by": admin["_id"],
            }, **sess(session))
            await db.vendor_users.insert_one({
                "_id": user_id,
                "vendor_id": vendor_id,
                "username": username,
                "email": email,
                "password_hash": None,  # set by the vendor through the invite link
                "status": "invited",
                "setup": {"token_hash": token_hash,
                          "expires_at": now + timedelta(hours=s.set_password_ttl_hours)},
                "created_at": now,
            }, **sess(session))
            await set_status(db, app, "accepted", admin, note, session, vendor_id=vendor_id)
    except DuplicateKeyError:
        raise HTTPException(409, "This application was already accepted or the login already exists")

    await audit(db, admin, "application.accept", "vendor_application", app["_id"],
                vendor_id=vendor_id, username=username, commission_pct=commission)
    link = await send_invite(db, email, username, company["legal_name"], raw_token)
    result = {"vendor_id": vendor_id, "username": username, "email": email,
              "invite_expires_at": now + timedelta(hours=s.set_password_ttl_hours),
              "commission_pct": commission}
    if s.expose_invite_links:
        result["invite_link"] = link
    return result


async def send_invite(db: Any, email: str, username: str, company: str, raw_token: str) -> str:
    s = get_settings()
    link = f"{s.frontend_base_url}/vendor/set-password?token={raw_token}"
    await send_email(
        db, email, "Your SuppAI vendor account is ready",
        f"Hello {company},\n\nYour application to sell on SuppAI has been accepted.\n\n"
        f"Username: {username}\n"
        f"Set your password here (link valid for {s.set_password_ttl_hours} hours):\n{link}\n\n"
        f"Then sign in at {s.frontend_base_url}/vendor/login\n\n— Team SuppAI",
        kind="vendor_invite",
    )
    return link


async def reissue_login(db: Any, vendor_id: str, admin: dict | None) -> dict:
    """'New password' in the admin UI: invalidates the current password and mails a fresh link."""
    s = get_settings()
    user = await db.vendor_users.find_one({"vendor_id": vendor_id})
    vendor = await db.vendors.find_one({"_id": vendor_id})
    if not user or not vendor:
        raise HTTPException(404, "Vendor login not found")
    raw, token_hash = new_secret_token()
    expires = utcnow() + timedelta(hours=s.set_password_ttl_hours)
    await db.vendor_users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": None, "status": "invited",
                  "setup": {"token_hash": token_hash, "expires_at": expires},
                  "password_changed_at": utcnow()}},
    )
    if admin:
        await audit(db, admin, "vendor.reissue_login", "vendor", vendor_id)
    link = await send_invite(db, user["email"], user["username"], vendor["legal_name"], raw)
    res = {"username": user["username"], "email": user["email"], "invite_expires_at": expires}
    if s.expose_invite_links:
        res["invite_link"] = link
    return res
