"""Admin → Vendor catalogue: listing requests on top, live vendor products below."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import current_admin, db_dep
from app.schemas.products import ApproveIn, HsnRateIn, ReviewNote
from app.services import products as svc
from app.services.common import audit, send_email
from app.utils import out, utcnow

router = APIRouter(prefix="/admin", tags=["Admin · vendor catalogue"])

PENDING = {"$or": [{"status": "awaiting_review"}, {"pending_edit.status": "awaiting_review"}]}


async def _get(db: Any, product_id: str) -> dict:
    p = await db.products.find_one({"_id": product_id})
    if not p:
        raise HTTPException(404, "Product not found")
    return p


def _row(p: dict) -> dict:
    d = svc.vendor_view(p)
    d["request_type"] = ("edit" if (p.get("pending_edit") or {}).get("status") == "awaiting_review"
                         else "new" if p["status"] == "awaiting_review" else None)
    return d


@router.get("/catalogue", summary="Listing requests (status=pending) or live products")
async def catalogue(status: Literal["pending", "listed", "changes_requested", "rejected", "removed", "all"] = "pending",
                    vendor_id: str | None = None, q: str | None = None,
                    page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100),
                    admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    filt: dict = dict(PENDING) if status == "pending" else ({} if status == "all" else {"status": status})
    if vendor_id:
        filt["vendor_id"] = vendor_id
    if q:
        filt["name"] = {"$regex": q, "$options": "i"}
    total = await db.products.count_documents(filt)
    sort_key = "submitted_at" if status == "pending" else "updated_at"
    items = [_row(p) async for p in db.products.find(filt).sort(sort_key, -1).skip((page - 1) * limit).limit(limit)]
    return {"items": items, "total": total, "page": page,
            "pending_count": await db.products.count_documents(PENDING)}


@router.get("/catalogue/{product_id}", summary="Photos, description, pricing, margin and history")
async def review_detail(product_id: str, admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    p = await _get(db, product_id)
    vendor = await db.vendors.find_one({"_id": p["vendor_id"]}, {"bank": 0})
    d = {**_row(p), "vendor": out(vendor), "history": await svc.history(db, product_id)}
    pe = p.get("pending_edit")
    if pe:
        proposed = await svc.prepare_content(db, p["vendor_id"], {**svc.content_of(p), **pe["changes"]})
        proposed_full = {**p, **proposed}
        proposed_full.update(svc.pricing(proposed_full))
        d["proposed"] = svc.vendor_view(proposed_full)
        keys = [*pe["changes"], "gst_pct", "final_price", "margin", "margin_pct"]
        d["diff"] = {k: {"from": p.get(k), "to": proposed_full.get(k)} for k in dict.fromkeys(keys)
                     if p.get(k) != proposed_full.get(k)}
    return d


async def _notify(db: Any, p: dict, subject: str, body: str) -> None:
    vendor = await db.vendors.find_one({"_id": p["vendor_id"]})
    if vendor:
        await send_email(db, vendor["official_email"], subject, body, kind="listing_review")


@router.post("/catalogue/{product_id}/approve", summary="List it in the Shop")
async def approve(product_id: str, body: ApproveIn | None = None, admin: dict = Depends(current_admin),
                  db: Any = Depends(db_dep)) -> dict:
    p = await _get(db, product_id)
    note = body.note if body else None
    actor, now = {"type": "admin", "id": admin["_id"]}, utcnow()
    pe = p.get("pending_edit")
    if pe and pe["status"] == "awaiting_review":
        content = await svc.prepare_content(db, p["vendor_id"], {**svc.content_of(p), **pe["changes"]})
        upd = {**content, "pending_edit": None, "review_note": note, "updated_at": now, "edit_approved_at": now}
        upd.update(svc.pricing({**p, **upd}))
        await db.products.update_one({"_id": p["_id"]}, {"$set": upd})
        await svc.log_event(db, p, "edit_approved", actor, note, is_edit=True)
        what = "Your changes are now live"
    elif p["status"] == "awaiting_review":
        await db.products.update_one({"_id": p["_id"], "status": "awaiting_review"}, {"$set": {
            "status": "listed", "review_note": note, "listed_at": now, "updated_at": now}})
        await svc.log_event(db, p, "approved", actor, note)
        what = "It is now live in the SuppAI Shop"
    else:
        raise HTTPException(409, "Nothing awaiting review on this product")
    await audit(db, admin, "catalogue.approve", "product", p["_id"], edit=bool(pe))
    await _notify(db, p, f"Approved: {p['name']}", f"{p['name']} was approved. {what}.\n\n{note or ''}")
    return _row(await _get(db, product_id))


@router.post("/catalogue/{product_id}/changes", summary="Send back with a note")
async def request_changes(product_id: str, body: ReviewNote, admin: dict = Depends(current_admin),
                          db: Any = Depends(db_dep)) -> dict:
    p = await _get(db, product_id)
    actor, now = {"type": "admin", "id": admin["_id"]}, utcnow()
    pe = p.get("pending_edit")
    if pe and pe["status"] == "awaiting_review":
        await db.products.update_one({"_id": p["_id"]}, {"$set": {
            "pending_edit.status": "changes_requested", "pending_edit.note": body.note, "updated_at": now}})
        await svc.log_event(db, p, "edit_changes_requested", actor, body.note, is_edit=True)
    elif p["status"] == "awaiting_review":
        await db.products.update_one({"_id": p["_id"]}, {"$set": {
            "status": "changes_requested", "review_note": body.note, "updated_at": now}})
        await svc.log_event(db, p, "changes_requested", actor, body.note)
    else:
        raise HTTPException(409, "Nothing awaiting review on this product")
    await audit(db, admin, "catalogue.changes", "product", p["_id"], note=body.note)
    await _notify(db, p, f"Changes needed: {p['name']}",
                  f"Please update {p['name']} and resubmit:\n\n{body.note}")
    return _row(await _get(db, product_id))


@router.post("/catalogue/{product_id}/reject", summary="Reject with a reason")
async def reject(product_id: str, body: ReviewNote, admin: dict = Depends(current_admin),
                 db: Any = Depends(db_dep)) -> dict:
    p = await _get(db, product_id)
    actor, now = {"type": "admin", "id": admin["_id"]}, utcnow()
    pe = p.get("pending_edit")
    if pe and pe["status"] == "awaiting_review":
        # Rejecting an edit drops the edit; the live product stays as it was.
        await db.products.update_one({"_id": p["_id"]}, {"$set": {
            "pending_edit": None, "review_note": body.note, "updated_at": now}})
        await svc.log_event(db, p, "edit_rejected", actor, body.note, is_edit=True)
    elif p["status"] in ("awaiting_review", "listed"):
        # A live product can also be pulled from the Shop.
        await db.products.update_one({"_id": p["_id"]}, {"$set": {
            "status": "rejected", "review_note": body.note, "pending_edit": None, "updated_at": now}})
        await svc.log_event(db, p, "rejected", actor, body.note)
    else:
        raise HTTPException(409, f"A {p['status']} product cannot be rejected")
    await audit(db, admin, "catalogue.reject", "product", p["_id"], note=body.note)
    await _notify(db, p, f"Not approved: {p['name']}", f"{p['name']} was not approved.\n\nReason: {body.note}")
    return _row(await _get(db, product_id))


# ---- GST rate table used to fill GST from the HSN code ---------------------------

@router.get("/hsn-rates")
async def list_hsn(admin: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> list[dict]:
    return [out(r) async for r in db.hsn_rates.find({}).sort("_id", 1)]


@router.put("/hsn-rates/{hsn_code}")
async def put_hsn(hsn_code: str, body: HsnRateIn, admin: dict = Depends(current_admin),
                  db: Any = Depends(db_dep)) -> dict:
    if not (hsn_code.isdigit() and len(hsn_code) in (4, 6, 8)):
        raise HTTPException(422, "HSN code must be 4, 6 or 8 digits")
    await db.hsn_rates.update_one({"_id": hsn_code}, {"$set": {**body.model_dump(), "updated_at": utcnow(),
                                                               "updated_by": admin["_id"]}}, upsert=True)
    await audit(db, admin, "hsn_rate.set", "hsn_rate", hsn_code, gst_pct=body.gst_pct)
    return out(await db.hsn_rates.find_one({"_id": hsn_code}))
