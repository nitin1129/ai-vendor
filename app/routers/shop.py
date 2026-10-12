"""/dashboard/shop — only Listed products are shown."""
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.deps import db_dep
from app.schemas.applications import CATEGORIES
from app.services.products import shop_view

router = APIRouter(prefix="/shop", tags=["Shop"])

SORTS = {
    "relevance": None,
    "newest": [("listed_at", -1)],
    "price_asc": [("final_price", 1)],
    "price_desc": [("final_price", -1)],
    "discount": [("discount_off_mrp_pct", -1)],
}


@router.get("/products", summary="Shop listing and search")
async def list_products(category: str | None = None, q: str | None = Query(None, max_length=100),
                        brand: str | None = None, vendor_id: str | None = None,
                        min_price: float | None = None, max_price: float | None = None,
                        in_stock_only: bool = False,
                        sort: Literal["relevance", "newest", "price_asc", "price_desc", "discount"] = "relevance",
                        page: int = Query(1, ge=1), limit: int = Query(24, ge=1, le=60),
                        db: Any = Depends(db_dep)) -> dict:
    filt: dict = {"status": "listed"}
    if category:
        filt["category"] = category
    if brand:
        filt["brand"] = {"$regex": f"^{re.escape(brand)}$", "$options": "i"}
    if vendor_id:
        filt["vendor_id"] = vendor_id
    price: dict = {}
    if min_price is not None:
        price["$gte"] = min_price
    if max_price is not None:
        price["$lte"] = max_price
    if price:
        filt["final_price"] = price
    if in_stock_only:
        filt.update({"in_stock": True, "stock": {"$gt": 0}})

    projection = None
    order = SORTS[sort]
    if q:
        if get_settings().search_mode == "text":
            filt["$text"] = {"$search": q}
            projection = {"score": {"$meta": "textScore"}}
            if order is None:
                order = [("score", {"$meta": "textScore"})]
        else:
            rx = {"$regex": re.escape(q), "$options": "i"}
            filt["$or"] = [{"name": rx}, {"brand": rx}, {"category": rx}, {"tags": rx}]
    order = order or [("listed_at", -1)]

    total = await db.products.count_documents(filt)
    cur = db.products.find(filt, projection).sort(order).skip((page - 1) * limit).limit(limit)
    return {"items": [shop_view(p) async for p in cur], "total": total, "page": page, "limit": limit}


@router.get("/products/{product_id}")
async def get_product(product_id: str, db: Any = Depends(db_dep)) -> dict:
    p = await db.products.find_one({"_id": product_id, "status": "listed"})
    if not p:
        raise HTTPException(404, "Product not found")
    return shop_view(p)


@router.get("/categories", summary="Categories with live product counts")
async def categories(db: Any = Depends(db_dep)) -> list[dict]:
    res = []
    for c in CATEGORIES:
        n = await db.products.count_documents({"status": "listed", "category": c})
        res.append({"category": c, "count": n})
    return res
