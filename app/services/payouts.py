"""Pay vendors after delivery + return window, minus commission, on their settlement cycle."""
from datetime import timedelta
from typing import Any

from app.services.applications import CYCLE_DAYS
from app.utils import as_utc, mask, money, new_id, utcnow


async def run_payouts(db: Any, now=None) -> list[dict]:
    now = now or utcnow()
    created = []
    async for vendor in db.vendors.find({"next_payout_at": {"$lte": now}}):
        orders = [o async for o in db.orders.find({
            "vendor_id": vendor["_id"], "status": "delivered",
            "return_window_ends_at": {"$lte": now}, "payout_id": None})]

        # Advance the cycle even when there is nothing to pay.
        nxt = as_utc(vendor["next_payout_at"])
        step = timedelta(days=CYCLE_DAYS.get(vendor.get("settlement_cycle"), 7))
        while nxt <= now:
            nxt += step
        await db.vendors.update_one({"_id": vendor["_id"]}, {"$set": {"next_payout_at": nxt}})
        if not orders:
            continue

        gross = money(sum(o["subtotal"] for o in orders))
        pct = float(vendor.get("commission_pct", 0))
        commission = money(gross * pct / 100)
        payout = {
            "_id": new_id("pay"), "vendor_id": vendor["_id"], "vendor_name": vendor.get("display_name"),
            "order_ids": [o["_id"] for o in orders], "order_count": len(orders),
            "gross": gross, "commission_pct": pct, "commission": commission, "net": money(gross - commission),
            "bank": {"account_holder": vendor["bank"]["account_holder"],
                     "account_number": mask(vendor["bank"]["account_number"]),
                     "ifsc": vendor["bank"]["ifsc"]},
            "status": "pending", "created_at": now, "paid_at": None, "utr": None,
        }
        await db.payouts.insert_one(payout)
        await db.orders.update_many({"_id": {"$in": payout["order_ids"]}},
                                    {"$set": {"payout_id": payout["_id"], "payout_status": "scheduled"}})
        created.append(payout)
    return created
