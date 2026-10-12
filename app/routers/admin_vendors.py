"""Admin → Onboarding → Vendors, plus vendor account management."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.deps import current_admin, db_dep
from app.schemas.applications import AcceptIn, AdminNote
from app.security import signed_file_url
from app.services import applications as svc
from app.services.common import audit, send_email
from app.utils import mask, out

router = APIRouter(prefix="/admin", tags=["Admin · vendor onboarding"])

AppStatus = Literal["submitted", "reviewing", "on_hold", "accepted", "rejected"]


async def _get_app(db: Any, app_id: str) -> dict:
    app = await db.vendor_applications.find_one({"_id": app_id, "status": {"$ne": "draft"}})
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@router.get("/vendors/applications", summary="List applications")
async def list_applications(status: AppStatus | None = None, q: str | None = None,
                            page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                            admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    filt: dict = {"status": status} if status else {"status": {"$ne": "draft"}}
    if q:
        filt["$or"] = [{"reference": {"$regex": q, "$options": "i"}},
                       {"steps.company.legal_name": {"$regex": q, "$options": "i"}}]
    total = await db.vendor_applications.count_documents(filt)
    cur = db.vendor_applications.find(filt).sort("submitted_at", -1).skip((page - 1) * limit).limit(limit)
    items = []
    async for a in cur:
        company = a.get("steps", {}).get("company", {})
        items.append({"id": a["_id"], "reference": a.get("reference"), "status": a["status"],
                      "legal_name": company.get("legal_name"), "contact_name": company.get("contact_name"),
                      "official_email": company.get("official_email"),
                      "categories": a.get("steps", {}).get("statutory", {}).get("categories", []),
                      "submitted_at": a.get("submitted_at"), "vendor_id": a.get("vendor_id")})
    counts = {}
    for st in AppStatus.__args__:  # type: ignore[attr-defined]
        counts[st] = await db.vendor_applications.count_documents({"status": st})
    return {"items": items, "total": total, "page": page, "counts": counts}


@router.get("/vendors/applications/{app_id}", summary="Every answer and the uploaded documents")
async def get_application(app_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    app = await _get_app(db, app_id)
    d = svc.admin_view(app, full=True)
    d["documents"] = [{k: v for k, v in doc.items() if k != "file_id"} for doc in app.get("documents", [])]
    d["problems"] = svc.missing_for_submit(app)
    return d


@router.get("/vendors/applications/{app_id}/documents/{doc_id}/link",
            summary="Short-lived link to view a private document")
async def document_link(app_id: str, doc_id: str, admin: dict = Depends(current_admin),
                        db: Any = Depends(db_dep)) -> dict:
    app = await _get_app(db, app_id)
    doc = next((d for d in app.get("documents", []) if d["id"] == doc_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    url, exp = signed_file_url(doc["file_id"])
    await audit(db, admin, "application.view_document", "vendor_application", app_id, doc_type=doc["doc_type"])
    return {"url": url, "expires_at": exp}


@router.post("/vendors/applications/{app_id}/review", summary="Mark reviewing")
async def mark_reviewing(app_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    app = await _get_app(db, app_id)
    await svc.set_status(db, app, "reviewing", admin, None)
    await audit(db, admin, "application.reviewing", "vendor_application", app_id)
    return svc.admin_view(await _get_app(db, app_id))


@router.post("/vendors/applications/{app_id}/hold", summary="Put on hold with a note")
async def hold(app_id: str, body: AdminNote, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    app = await _get_app(db, app_id)
    await svc.set_status(db, app, "on_hold", admin, body.note)
    await audit(db, admin, "application.hold", "vendor_application", app_id, note=body.note)
    company = app["steps"]["company"]
    await send_email(db, company["official_email"], f"Your SuppAI application {app['reference']} is on hold",
                     f"Hello {company['contact_name']},\n\nWe need a little more before we can continue:\n\n"
                     f"{body.note}\n\nPlease reply to this email.\n\n— Team SuppAI", kind="application_hold")
    return svc.admin_view(await _get_app(db, app_id))


@router.post("/vendors/applications/{app_id}/reject", summary="Reject with a note")
async def reject(app_id: str, body: AdminNote, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    app = await _get_app(db, app_id)
    await svc.set_status(db, app, "rejected", admin, body.note)
    await audit(db, admin, "application.reject", "vendor_application", app_id, note=body.note)
    company = app["steps"]["company"]
    await send_email(db, company["official_email"], f"Your SuppAI application {app['reference']}",
                     f"Hello {company['contact_name']},\n\nWe are unable to onboard you at this time.\n\n"
                     f"Reason: {body.note}\n\n— Team SuppAI", kind="application_rejected")
    return svc.admin_view(await _get_app(db, app_id))


@router.post("/vendors/applications/{app_id}/accept", summary="Accept & issue login")
async def accept(app_id: str, body: AcceptIn | None = None, admin: dict = Depends(current_admin),
                 db: Any = Depends(db_dep)) -> dict:
    body = body or AcceptIn()
    app = await _get_app(db, app_id)
    return await svc.accept(db, app, admin, body.commission_pct, body.note)


# ---- Vendor accounts ------------------------------------------------------------

def _vendor_view(v: dict) -> dict:
    d = out(v)
    if d.get("bank"):
        d["bank"] = {**d["bank"], "account_number": mask(d["bank"].get("account_number"))}
    return d


@router.get("/vendors", summary="List vendor accounts")
async def list_vendors(status: Literal["active", "suspended"] | None = None,
                       admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> list[dict]:
    filt = {"status": status} if status else {}
    vendors = []
    async for v in db.vendors.find(filt).sort("created_at", -1):
        user = await db.vendor_users.find_one({"vendor_id": v["_id"]})
        vendors.append({**_vendor_view(v), "username": user and user["username"],
                        "login_status": user and user["status"]})
    return vendors


@router.get("/vendors/{vendor_id}")
async def get_vendor(vendor_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    v = await db.vendors.find_one({"_id": vendor_id})
    if not v:
        raise HTTPException(404, "Vendor not found")
    user = await db.vendor_users.find_one({"vendor_id": vendor_id})
    return {**_vendor_view(v), "username": user and user["username"], "login_status": user and user["status"]}


@router.post("/vendors/{vendor_id}/reissue-login", summary="'New password': mail a fresh set-password link")
async def reissue_login(vendor_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    return await svc.reissue_login(db, vendor_id, admin)


@router.post("/vendors/{vendor_id}/send-invite", summary="'Send invite email' again")
async def send_invite(vendor_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    user = await db.vendor_users.find_one({"vendor_id": vendor_id})
    if user and user["status"] == "active":
        raise HTTPException(409, "The vendor has already set a password; use reissue-login to reset it")
    return await svc.reissue_login(db, vendor_id, admin)


class VendorPatch(BaseModel):
    commission_pct: float | None = Field(default=None, ge=0, le=60)
    status: Literal["active", "suspended"] | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.patch("/vendors/{vendor_id}", summary="Change commission or suspend / reactivate")
async def patch_vendor(vendor_id: str, body: VendorPatch, admin: dict = Depends(current_admin),
                       db: Any = Depends(db_dep)) -> dict:
    changes = body.model_dump(exclude_none=True, exclude={"note"})
    if not changes:
        raise HTTPException(422, "Nothing to change")
    res = await db.vendors.update_one({"_id": vendor_id}, {"$set": changes})
    if not res.matched_count:
        raise HTTPException(404, "Vendor not found")
    await audit(db, admin, "vendor.update", "vendor", vendor_id, **changes, note=body.note)
    return await get_vendor(vendor_id, admin, db)


@router.get("/audit-logs", summary="Who did what, and when")
async def audit_logs(target_id: str | None = None, limit: int = Query(100, le=500),
                     admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> list[dict]:
    filt = {"target_id": target_id} if target_id else {}
    return [out(a) async for a in db.audit_logs.find(filt).sort("at", -1).limit(limit)]
