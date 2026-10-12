"""Checkout, payment confirmation and the order status machine.

A cart is split by vendor: one checkout (one Razorpay payment) -> one order per vendor.
Stock is taken down at checkout so two members cannot buy the last unit; it goes back
when the payment window expires or the order is cancelled.
"""
from collections import defaultdict
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from pymongo import ReturnDocument

from app.config import get_settings
from app.db import sess, transaction
from app.services import payments
from app.services.common import log, send_email
from app.services.products import is_buyable
from app.utils import money, new_id, next_seq, out, utcnow

# Vendor-driven transitions (doc §4 Orders tab).
VENDOR_TRANSITIONS = {
    "placed": {"packed", "cancelled"},
    "packed": {"shipped", "cancelled"},
    "shipped": {"delivered"},
}

MEMBER_LABELS = {
    "payment_pending": "Awaiting payment",
    "placed": "Order placed",
    "packed": "Packed",
    "shipped": "Shipped",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "expired": "Payment not completed",
}
TRACKER_STEPS = ["placed", "packed", "shipped", "delivered"]
LIVE_STATUSES = ["placed", "packed", "shipped", "delivered"]


def _history(status: str, by: str, note: str | None = None) -> dict:
    return {"status": status, "at": utcnow(), "by": by, "note": note}


async def _release_stock(db: Any, items: list[dict], session: Any = None) -> None:
    for it in items:
        await db.products.update_one({"_id": it["product_id"]}, {"$inc": {"stock": it["qty"]}}, **sess(session))


async def checkout(db: Any, member_id: str, lines: list[dict], shipping_address: dict, contact: dict) -> dict:
    s = get_settings()
    qty_by_product: dict[str, int] = defaultdict(int)
    for line in lines:
        qty_by_product[line["product_id"]] += line["qty"]

    products = {p["_id"]: p async for p in db.products.find({"_id": {"$in": list(qty_by_product)}})}
    problems = []
    for pid, qty in qty_by_product.items():
        p = products.get(pid)
        if not p or not is_buyable(p):
            problems.append({"product_id": pid, "problem": "not available"})
        elif p["stock"] < qty:
            problems.append({"product_id": pid, "problem": f"only {p['stock']} left"})
    if problems:
        raise HTTPException(409, {"message": "Some items are unavailable", "items": problems})

    # Reserve stock atomically per product; undo on any failure.
    reserved: list[dict] = []
    try:
        for pid, qty in qty_by_product.items():
            res = await db.products.find_one_and_update(
                {"_id": pid, "status": "listed", "in_stock": True, "stock": {"$gte": qty}},
                {"$inc": {"stock": -qty}}, return_document=ReturnDocument.AFTER)
            if res is None:
                raise HTTPException(409, {"message": "Some items just sold out",
                                          "items": [{"product_id": pid, "problem": "sold out"}]})
            reserved.append({"product_id": pid, "qty": qty})
    except Exception:
        await _release_stock(db, reserved)
        raise

    now = utcnow()
    expires = now + timedelta(minutes=s.payment_pending_ttl_minutes)
    checkout_id = new_id("chk")
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for pid, qty in qty_by_product.items():
        p = products[pid]
        by_vendor[p["vendor_id"]].append({
            "product_id": pid, "name": p["name"], "brand": p["brand"],
            "image": (p.get("images") or [{}])[0].get("thumb"),
            "qty": qty, "unit_price": p["final_price"], "mrp": p["mrp"], "gst_pct": p["gst_pct"],
            "hsn_code": p["hsn_code"], "line_total": money(p["final_price"] * qty),
        })

    orders = []
    for vendor_id, items in by_vendor.items():
        orders.append({
            "_id": new_id("ord"),
            "number": f"SA-ORD-{await next_seq(db, 'order', start=100000)}",
            "checkout_id": checkout_id, "member_id": member_id, "vendor_id": vendor_id,
            "vendor_name": products[items[0]["product_id"]].get("vendor_name"),
            "items": items, "subtotal": money(sum(i["line_total"] for i in items)),
            "units": sum(i["qty"] for i in items),
            "status": "payment_pending", "status_history": [_history("payment_pending", "member")],
            "shipping_address": shipping_address, "contact": contact,
            "shipment": None, "payout_id": None, "payout_status": None,
            "created_at": now, "updated_at": now,
        })
    total = money(sum(o["subtotal"] for o in orders))
    amount_paise = int(round(total * 100))

    try:
        rp = await payments.create_order(amount_paise, checkout_id, {"checkout_id": checkout_id})
    except payments.PaymentError:
        await _release_stock(db, reserved)
        raise HTTPException(502, "Payment gateway is unavailable, please try again")

    async with transaction() as session:
        await db.orders.insert_many(orders, **sess(session))
        await db.checkouts.insert_one({
            "_id": checkout_id, "member_id": member_id, "order_ids": [o["_id"] for o in orders],
            "items": reserved, "amount": total, "amount_paise": amount_paise, "currency": "INR",
            "razorpay_order_id": rp["id"], "status": "created", "payment_id": None,
            "created_at": now, "expires_at": expires,
        }, **sess(session))

    return {
        "checkout_id": checkout_id, "amount": total, "amount_paise": amount_paise, "currency": "INR",
        "razorpay_order_id": rp["id"], "razorpay_key_id": s.razorpay_key_id or "mock",
        "expires_at": expires, "orders": [member_view(o) for o in orders],
    }


async def confirm_payment(db: Any, razorpay_order_id: str, payment_id: str) -> dict:
    """Idempotent. Called by the webhook and by the browser verify call, whichever is first."""
    now = utcnow()
    chk = await db.checkouts.find_one_and_update(
        {"razorpay_order_id": razorpay_order_id, "status": "created"},
        {"$set": {"status": "paid", "payment_id": payment_id, "paid_at": now}},
        return_document=ReturnDocument.AFTER)
    if chk is None:
        chk = await db.checkouts.find_one({"razorpay_order_id": razorpay_order_id})
        if chk is None:
            raise HTTPException(404, "Unknown payment order")
        if chk["status"] == "expired":
            # Paid after we released the stock: refund in full rather than oversell.
            rf = await payments.refund(payment_id, chk["amount_paise"], {"reason": "payment after expiry"})
            await db.checkouts.update_one({"_id": chk["_id"]}, {"$set": {
                "status": "refunded_late", "payment_id": payment_id, "refund": rf, "updated_at": now}})
            log.warning("Late payment %s on expired checkout %s refunded", payment_id, chk["_id"])
        return out(chk)

    await db.orders.update_many(
        {"checkout_id": chk["_id"], "status": "payment_pending"},
        {"$set": {"status": "placed", "placed_at": now, "updated_at": now, "payment_id": payment_id},
         "$push": {"status_history": _history("placed", "system")}})
    async for o in db.orders.find({"checkout_id": chk["_id"]}):
        vendor = await db.vendors.find_one({"_id": o["vendor_id"]})
        if vendor:
            lines = "\n".join(f"- {i['name']} × {i['qty']}" for i in o["items"])
            await send_email(db, vendor["official_email"], f"New order {o['number']}",
                             f"You have a new SuppAI order {o['number']}:\n\n{lines}\n\n"
                             "Please pack and ship it from your vendor dashboard.", kind="vendor_new_order")
    return out(chk)


async def expire_stale(db: Any) -> int:
    """Releases stock for checkouts that were never paid. Run every few minutes."""
    n = 0
    async for chk in db.checkouts.find({"status": "created", "expires_at": {"$lt": utcnow()}}):
        res = await db.checkouts.update_one({"_id": chk["_id"], "status": "created"},
                                            {"$set": {"status": "expired", "expired_at": utcnow()}})
        if not res.modified_count:
            continue  # paid in the meantime
        await _release_stock(db, chk["items"])
        await db.orders.update_many(
            {"checkout_id": chk["_id"], "status": "payment_pending"},
            {"$set": {"status": "expired", "updated_at": utcnow()},
             "$push": {"status_history": _history("expired", "system")}})
        n += 1
    return n


async def cancel_order(db: Any, order: dict, by: str, reason: str) -> dict:
    """Refund this vendor's part of the payment and put the stock back."""
    now = utcnow()
    res = await db.orders.find_one_and_update(
        {"_id": order["_id"], "status": order["status"]},
        {"$set": {"status": "cancelled", "cancelled_at": now, "cancel_reason": reason, "updated_at": now},
         "$push": {"status_history": _history("cancelled", by, reason)}},
        return_document=ReturnDocument.AFTER)
    if res is None:
        raise HTTPException(409, "The order changed in the meantime; reload and try again")
    await _release_stock(db, order["items"])
    refund = {"_id": new_id("rfd"), "order_id": order["_id"], "amount": order["subtotal"],
              "payment_id": order.get("payment_id"), "created_at": now}
    try:
        rf = await payments.refund(order.get("payment_id"), int(round(order["subtotal"] * 100)),
                                   {"order": order["number"]})
        refund.update(status="processed", razorpay_refund_id=rf.get("id"))
    except payments.PaymentError as e:
        refund.update(status="failed", error=str(e))  # visible to admin for a manual retry
    await db.refunds.insert_one(refund)
    await db.orders.update_one({"_id": order["_id"]}, {"$set": {"refund_status": refund["status"],
                                                                "refund_id": refund["_id"]}})
    return await db.orders.find_one({"_id": order["_id"]})


async def vendor_move(db: Any, order: dict, vendor: dict, to: str, courier: str | None,
                      tracking_number: str | None, tracking_url: str | None, note: str | None) -> dict:
    allowed = VENDOR_TRANSITIONS.get(order["status"], set())
    if to not in allowed:
        raise HTTPException(409, f"Cannot move an order from '{order['status']}' to '{to}'")
    if to == "cancelled":
        if not note:
            raise HTTPException(422, "Give a reason to cancel")
        return await cancel_order(db, order, f"vendor:{vendor['_id']}", note)

    now = utcnow()
    upd: dict = {"status": to, f"{to}_at": now, "updated_at": now}
    if to == "shipped":
        if not (courier and tracking_number):
            raise HTTPException(422, "Courier and tracking number are required to mark shipped")
        shipment = {"_id": new_id("shp"), "order_id": order["_id"], "vendor_id": vendor["_id"],
                    "courier": courier, "tracking_number": tracking_number, "tracking_url": tracking_url,
                    "shipped_at": now}
        await db.shipments.insert_one(shipment)
        upd["shipment"] = out(shipment)
    if to == "delivered":
        upd["return_window_ends_at"] = now + timedelta(days=vendor.get("return_window_days", 0))
        upd["payout_status"] = "pending"
    res = await db.orders.find_one_and_update(
        {"_id": order["_id"], "status": order["status"]},
        {"$set": upd, "$push": {"status_history": _history(to, f"vendor:{vendor['_id']}", note)}},
        return_document=ReturnDocument.AFTER)
    if res is None:
        raise HTTPException(409, "The order changed in the meantime; reload and try again")
    return res


def member_view(o: dict) -> dict:
    """The member's order tracker moves with the vendor's status."""
    reached = {h["status"]: h["at"] for h in o.get("status_history", [])}
    steps = [{"status": st, "label": MEMBER_LABELS[st], "done": st in reached, "at": reached.get(st)}
             for st in TRACKER_STEPS]
    shipment = o.get("shipment") or None
    return {
        "id": o["_id"], "number": o["number"], "status": o["status"], "label": MEMBER_LABELS[o["status"]],
        "vendor_name": o.get("vendor_name"), "items": o["items"], "subtotal": o["subtotal"],
        "tracker": steps, "shipment": shipment and {k: shipment.get(k) for k in
                                                    ("courier", "tracking_number", "tracking_url", "shipped_at")},
        "shipping_address": o.get("shipping_address"), "created_at": o["created_at"],
        "refund_status": o.get("refund_status"),
    }
