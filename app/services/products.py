"""One products collection feeds the vendor portal, the admin catalogue and the Shop.

Status            Shop
awaiting_review   hidden
changes_requested hidden
listed            shown (buyable unless out of stock)
rejected          hidden
removed           hidden (vendor removed it)

An edit to a listed product is staged in `pending_edit`; the live version stays in the
Shop until the admin approves the edit.
"""
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from app.config import get_settings
from app.schemas.products import CONTENT_FIELDS, ProductBase
from app.utils import money, new_id, out, utcnow


def image_urls(img: dict) -> dict:
    base = get_settings().public_base_url
    return {"id": img["_id"], **{size: f"{base}/files/images/{fid}" for size, fid in img["variants"].items()}}


async def resolve_images(db: Any, vendor_id: str, ids: list[str]) -> list[dict]:
    found = {i["_id"]: i async for i in db.product_images.find({"_id": {"$in": ids}, "vendor_id": vendor_id})}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(422, f"Unknown image id(s): {', '.join(missing)}")
    return [image_urls(found[i]) for i in ids]


async def resolve_gst(db: Any, hsn_code: str, given: float | None) -> float:
    """GST comes from the HSN rate table (longest matching prefix); vendor value is the fallback."""
    for n in (8, 6, 4):
        if len(hsn_code) >= n:
            rate = await db.hsn_rates.find_one({"_id": hsn_code[:n]})
            if rate:
                return float(rate["gst_pct"])
    if given is None:
        raise HTTPException(422, "This HSN code is not in our GST table; please enter GST %")
    return float(given)


def pricing(doc: dict) -> dict:
    final = money(doc["selling_price"] * (1 - doc.get("discount_pct", 0) / 100))
    margin = money(doc["selling_price"] - doc["supply_cost"])
    return {
        "final_price": final,
        "margin": margin,
        "margin_pct": money(margin / doc["selling_price"] * 100) if doc["selling_price"] else 0.0,
        "discount_off_mrp_pct": money((1 - final / doc["mrp"]) * 100) if doc["mrp"] else 0.0,
    }


def validate_content(data: dict) -> dict:
    try:
        return ProductBase.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))


async def prepare_content(db: Any, vendor_id: str, data: dict) -> dict:
    """Validates content fields, fills GST and expands image ids into URLs."""
    content = validate_content(data)
    content["gst_pct"] = await resolve_gst(db, content["hsn_code"], content.get("gst_pct"))
    content["images"] = await resolve_images(db, vendor_id, content["images"])
    return content


def content_of(product: dict) -> dict:
    d = {k: product.get(k) for k in CONTENT_FIELDS}
    d["images"] = [i["id"] for i in product.get("images", [])]
    return d


def is_buyable(p: dict) -> bool:
    return p.get("status") == "listed" and p.get("in_stock", True) and p.get("stock", 0) > 0


async def log_event(db: Any, product: dict, type_: str, actor: dict, note: str | None = None,
                    is_edit: bool = False, changes: dict | None = None) -> None:
    await db.listing_events.insert_one({
        "_id": new_id("evt"), "product_id": product["_id"], "vendor_id": product["vendor_id"],
        "type": type_, "note": note, "is_edit": is_edit, "changes": changes,
        "actor": actor, "at": utcnow(),
    })


async def history(db: Any, product_id: str) -> list[dict]:
    return [out(e) async for e in db.listing_events.find({"product_id": product_id}).sort("at", 1)]


def vendor_view(p: dict) -> dict:
    d = out(p)
    d["buyable"] = is_buyable(p)
    d["shop_label"] = shop_label(p)
    return d


def shop_label(p: dict) -> str:
    if p["status"] != "listed":
        return "Hidden"
    return "Shown and buyable" if is_buyable(p) else "Shown, cannot be bought"


def shop_view(p: dict) -> dict:
    """What members see: no supply cost, margin or internal review data."""
    return {
        "id": p["_id"], "name": p["name"], "brand": p["brand"], "category": p["category"],
        "tags": p.get("tags", []), "images": p.get("images", []),
        "short_description": p["short_description"], "description": p["description"],
        "ingredients": p["ingredients"], "dosage": p["dosage"],
        "mrp": p["mrp"], "price": p["final_price"], "discount_pct": p.get("discount_pct", 0),
        "discount_off_mrp_pct": p.get("discount_off_mrp_pct", 0), "gst_pct": p["gst_pct"],
        "hsn_code": p["hsn_code"], "stock": p.get("stock", 0) if p.get("in_stock", True) else 0,
        "in_stock": is_buyable(p),
        "vendor": {"id": p["vendor_id"], "name": p.get("vendor_name")},
        "delivery_days": p.get("delivery_days"), "listed_at": p.get("listed_at"),
    }
