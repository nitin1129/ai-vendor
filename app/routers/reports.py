"""Dashboard numbers, payouts and background jobs."""
from collections import defaultdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps import VendorPrincipal, current_admin, current_vendor, db_dep
from app.services.common import audit
from app.services.orders import LIVE_STATUSES, MEMBER_LABELS, expire_stale
from app.services.payouts import run_payouts
from app.utils import money, out, utcnow

vendor = APIRouter(prefix="/vendor", tags=["Vendor · dashboard"])
admin = APIRouter(prefix="/admin", tags=["Admin · payouts & jobs"])


@vendor.get("/reports/summary", summary="Numbers for the dashboard")
async def summary(me: VendorPrincipal = Depends(current_vendor), db: Any = Depends(db_dep)) -> dict:
    revenue, units, orders = 0.0, 0, 0
    per_product: dict[str, dict] = defaultdict(lambda: {"units": 0, "revenue": 0.0, "name": ""})
    async for o in db.orders.find({"vendor_id": me.vendor_id, "status": {"$in": LIVE_STATUSES}}):
        orders += 1
        revenue += o["subtotal"]
        units += o["units"]
        for it in o["items"]:
            pp = per_product[it["product_id"]]
            pp["units"] += it["qty"]
            pp["revenue"] += it["line_total"]
            pp["name"] = it["name"]

    products = [p async for p in db.products.find({"vendor_id": me.vendor_id, "status": {"$ne": "removed"}})]
    listed = [p for p in products if p["status"] == "listed"]
    health = {
        "listed": len(listed),
        "awaiting_review": sum(1 for p in products if p["status"] == "awaiting_review"
                               or (p.get("pending_edit") or {}).get("status") == "awaiting_review"),
        "changes_requested": sum(1 for p in products if p["status"] == "changes_requested"
                                 or (p.get("pending_edit") or {}).get("status") == "changes_requested"),
        "out_of_stock": sum(1 for p in listed if not p.get("in_stock", True) or p.get("stock", 0) <= 0),
    }
    top = max(per_product.items(), key=lambda kv: kv[1]["units"], default=None)
    recent = [{"id": o["_id"], "number": o["number"], "status": o["status"],
               "label": MEMBER_LABELS[o["status"]], "subtotal": o["subtotal"], "units": o["units"],
               "created_at": o["created_at"]}
              async for o in db.orders.find({"vendor_id": me.vendor_id,
                                             "status": {"$in": LIVE_STATUSES + ["cancelled"]}})
              .sort("created_at", -1).limit(5)]
    pending_payout = 0.0
    async for o in db.orders.find({"vendor_id": me.vendor_id, "status": "delivered", "payout_status": {"$ne": "paid"}}):
        pending_payout += o["subtotal"]
    return {
        "revenue": money(revenue), "orders": orders, "units_sold": units, "listed_products": len(listed),
        "catalogue_health": health,
        "top_seller": top and {"product_id": top[0], "name": top[1]["name"], "units": top[1]["units"],
                               "revenue": money(top[1]["revenue"])},
        "recent_orders": recent,
        "awaiting_payout_gross": money(pending_payout),
        "next_payout_at": me.vendor.get("next_payout_at"),
        "commission_pct": me.vendor.get("commission_pct"),
    }


@vendor.get("/payouts")
async def vendor_payouts(me: VendorPrincipal = Depends(current_vendor), db: Any = Depends(db_dep)) -> list[dict]:
    return [out(p) async for p in db.payouts.find({"vendor_id": me.vendor_id}).sort("created_at", -1)]


@vendor.get("/profile")
async def profile(me: VendorPrincipal = Depends(current_vendor), db: Any = Depends(db_dep)) -> dict:
    v = out(me.vendor)
    v["bank"] = {**v["bank"], "account_number": "•" * 6 + v["bank"]["account_number"][-4:]}
    user = await db.vendor_users.find_one({"_id": me.user_id})
    return {**v, "username": user and user["username"]}


# ---- Admin -------------------------------------------------------------------------

@admin.get("/payouts")
async def list_payouts(status: Literal["pending", "paid"] | None = None, vendor_id: str | None = None,
                       a: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> list[dict]:
    filt: dict = {}
    if status:
        filt["status"] = status
    if vendor_id:
        filt["vendor_id"] = vendor_id
    return [out(p) async for p in db.payouts.find(filt).sort("created_at", -1)]


class MarkPaidIn(BaseModel):
    utr: str = Field(min_length=6, max_length=40, description="Bank transfer reference")


@admin.post("/payouts/{payout_id}/mark-paid")
async def mark_paid(payout_id: str, body: MarkPaidIn, a: dict = Depends(current_admin),
                    db: Any = Depends(db_dep)) -> dict:
    res = await db.payouts.update_one({"_id": payout_id, "status": "pending"},
                                      {"$set": {"status": "paid", "utr": body.utr, "paid_at": utcnow(),
                                                "paid_by": a["_id"]}})
    if not res.modified_count:
        raise HTTPException(409, "Payout not found or already paid")
    await db.orders.update_many({"payout_id": payout_id}, {"$set": {"payout_status": "paid"}})
    await audit(db, a, "payout.mark_paid", "payout", payout_id, utr=body.utr)
    return out(await db.payouts.find_one({"_id": payout_id}))


@admin.post("/jobs/run", summary="Expire unpaid checkouts and create due payouts")
async def run_jobs(a: dict = Depends(current_admin), db: Any = Depends(db_dep)) -> dict:
    expired = await expire_stale(db)
    payouts = await run_payouts(db)
    await audit(db, a, "jobs.run", "system", "jobs", expired=expired, payouts=len(payouts))
    return {"expired_checkouts": expired, "payouts_created": [p["_id"] for p in payouts]}


@admin.get("/refunds")
async def refunds(status: Literal["processed", "failed"] | None = None, a: dict = Depends(current_admin),
                  db: Any = Depends(db_dep)) -> list[dict]:
    filt = {"status": status} if status else {}
    return [out(r) async for r in db.refunds.find(filt).sort("created_at", -1)]
