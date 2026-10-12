"""Vendor portal /vendor — Products and Request new listing."""
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config import get_settings
from app.deps import VendorPrincipal, current_vendor, db_dep
from app.schemas.products import ProductIn, ProductPatch, ProductStatus, StockPatch
from app.services import products as svc
from app.storage import BadImage, get_store, make_image_variants
from app.utils import new_id, utcnow

router = APIRouter(prefix="/vendor", tags=["Vendor · products"])


def _actor(me: VendorPrincipal) -> dict:
    return {"type": "vendor", "id": me.user_id}


async def _mine(db: Any, me: VendorPrincipal, product_id: str) -> dict:
    p = await db.products.find_one({"_id": product_id, "vendor_id": me.vendor_id})
    if not p:
        raise HTTPException(404, "Product not found")
    return p


@router.post("/uploads/images", status_code=201, summary="Upload a product photo (resized to fixed sizes)")
async def upload_image(file: UploadFile = File(...), me: VendorPrincipal = Depends(current_vendor),
                       db: Any = Depends(db_dep)) -> dict:
    data = await file.read()
    if len(data) > get_settings().max_image_mb * 1024 * 1024:
        raise HTTPException(413, "Image is too large")
    try:
        variants = make_image_variants(data)
    except BadImage:
        raise HTTPException(415, "Upload a JPG, PNG or WebP image")
    store = get_store()
    ids = {}
    for size, blob in variants.items():
        ids[size] = await store.put(blob, f"{size}.webp", "image/webp",
                                    {"kind": "product_image", "public": True, "vendor_id": me.vendor_id})
    img = {"_id": new_id("img"), "vendor_id": me.vendor_id, "variants": ids, "created_at": utcnow()}
    await db.product_images.insert_one(img)
    return svc.image_urls(img)


@router.get("/products", summary="Every listing with its status")
async def list_products(status: ProductStatus | None = None, me: VendorPrincipal = Depends(current_vendor),
                        db: Any = Depends(db_dep)) -> list[dict]:
    filt: dict = {"vendor_id": me.vendor_id}
    filt["status"] = status if status else {"$ne": "removed"}
    return [svc.vendor_view(p) async for p in db.products.find(filt).sort("updated_at", -1)]


@router.get("/products/{product_id}", summary="One listing with its review history")
async def get_product(product_id: str, me: VendorPrincipal = Depends(current_vendor),
                      db: Any = Depends(db_dep)) -> dict:
    p = await _mine(db, me, product_id)
    return {**svc.vendor_view(p), "history": await svc.history(db, product_id)}


@router.post("/products", status_code=201, summary="Request new listing (submit for review)")
async def create_product(body: ProductIn, me: VendorPrincipal = Depends(current_vendor),
                         db: Any = Depends(db_dep)) -> dict:
    data = body.model_dump(mode="json")
    content = await svc.prepare_content(db, me.vendor_id, data)
    now = utcnow()
    product = {
        "_id": new_id("prd"), "vendor_id": me.vendor_id, "vendor_name": me.vendor["display_name"],
        "delivery_days": me.vendor.get("delivery_days"),
        **content, "stock": data["stock"], "discount_pct": data["discount_pct"], "in_stock": True,
        "status": "awaiting_review", "review_note": None, "pending_edit": None,
        "created_at": now, "updated_at": now, "submitted_at": now, "listed_at": None,
    }
    product.update(svc.pricing(product))
    await db.products.insert_one(product)
    await svc.log_event(db, product, "submitted", _actor(me))
    return svc.vendor_view(product)


@router.patch("/products/{product_id}", summary="Edit and resubmit")
async def edit_product(product_id: str, body: ProductPatch, me: VendorPrincipal = Depends(current_vendor),
                       db: Any = Depends(db_dep)) -> dict:
    p = await _mine(db, me, product_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    if not changes:
        raise HTTPException(422, "Nothing to change")
    now = utcnow()

    if p["status"] in ("awaiting_review", "changes_requested"):
        merged = {**svc.content_of(p), **changes}
        content = await svc.prepare_content(db, me.vendor_id, merged)
        upd = {**content, "status": "awaiting_review", "updated_at": now, "submitted_at": now}
        upd.update(svc.pricing({**p, **upd}))
        await db.products.update_one({"_id": p["_id"]}, {"$set": upd})
        await svc.log_event(db, p, "resubmitted" if p["status"] == "changes_requested" else "updated",
                            _actor(me), changes=changes)

    elif p["status"] == "listed":
        # Stage the edit; the live version keeps selling until the admin approves.
        previous = (p.get("pending_edit") or {}).get("changes", {})
        all_changes = {**previous, **changes}
        merged = {**svc.content_of(p), **all_changes}
        await svc.prepare_content(db, me.vendor_id, merged)  # validate now, not at approval time
        await db.products.update_one({"_id": p["_id"]}, {"$set": {
            "pending_edit": {"changes": all_changes, "status": "awaiting_review",
                             "note": None, "submitted_at": now},
            "updated_at": now}})
        await svc.log_event(db, p, "edit_submitted", _actor(me), is_edit=True, changes=changes)
    else:
        raise HTTPException(409, f"A {p['status']} product cannot be edited; submit a new listing")

    return svc.vendor_view(await db.products.find_one({"_id": p["_id"]}))


@router.delete("/products/{product_id}/pending-edit", summary="Withdraw a staged edit")
async def withdraw_edit(product_id: str, me: VendorPrincipal = Depends(current_vendor),
                        db: Any = Depends(db_dep)) -> dict:
    p = await _mine(db, me, product_id)
    if not p.get("pending_edit"):
        raise HTTPException(404, "No pending edit")
    await db.products.update_one({"_id": p["_id"]}, {"$set": {"pending_edit": None, "updated_at": utcnow()}})
    await svc.log_event(db, p, "edit_withdrawn", _actor(me), is_edit=True)
    return svc.vendor_view(await db.products.find_one({"_id": p["_id"]}))


@router.patch("/products/{product_id}/stock", summary="In or out of stock, stock count, discount (no review)")
async def update_stock(product_id: str, body: StockPatch, me: VendorPrincipal = Depends(current_vendor),
                       db: Any = Depends(db_dep)) -> dict:
    p = await _mine(db, me, product_id)
    if p["status"] in ("rejected", "removed"):
        raise HTTPException(409, f"A {p['status']} product cannot be changed")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Nothing to change")
    upd = {**changes, "updated_at": utcnow()}
    upd.update(svc.pricing({**p, **changes}))
    await db.products.update_one({"_id": p["_id"]}, {"$set": upd})
    await svc.log_event(db, p, "stock_updated", _actor(me), changes=changes)
    return svc.vendor_view(await db.products.find_one({"_id": p["_id"]}))


@router.delete("/products/{product_id}", summary="Remove a listing")
async def remove_product(product_id: str, me: VendorPrincipal = Depends(current_vendor),
                         db: Any = Depends(db_dep)) -> dict:
    p = await _mine(db, me, product_id)
    if p["status"] == "removed":
        raise HTTPException(409, "Already removed")
    await db.products.update_one({"_id": p["_id"]}, {"$set": {
        "status": "removed", "pending_edit": None, "removed_at": utcnow(), "updated_at": utcnow()}})
    await svc.log_event(db, p, "removed", _actor(me))
    return {"id": p["_id"], "status": "removed"}

