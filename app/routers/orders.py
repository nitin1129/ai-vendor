"""Member checkout and order tracker, Razorpay webhook, and the vendor Orders tab."""
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.deps import VendorPrincipal, current_member, current_vendor, db_dep
from app.schemas.applications import PHONE_RE, Address
from app.services import orders as svc
from app.services import payments
from app.utils import new_id, out, utcnow

shop = APIRouter(prefix="/shop", tags=["Shop · checkout & orders"])
hooks = APIRouter(prefix="/payments", tags=["Payments"])
vendor = APIRouter(prefix="/vendor", tags=["Vendor · orders"])


class CartLine(BaseModel):
    product_id: str
    qty: int = Field(ge=1, le=50)


class Contact(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=PHONE_RE)
    email: str | None = None


class CheckoutIn(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=50)
    shipping_address: Address
    contact: Contact


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@shop.post("/checkout", status_code=201, summary="Cart -> one payment, one order per vendor")
async def checkout(body: CheckoutIn, member_id: str = Depends(current_member), db: Any = Depends(db_dep)) -> dict:
    return await svc.checkout(db, member_id, [l.model_dump() for l in body.items],
                              body.shipping_address.model_dump(), body.contact.model_dump())


@shop.post("/checkout/{checkout_id}/verify", summary="Browser confirms payment (webhook also does)")
async def verify(checkout_id: str, body: VerifyIn, member_id: str = Depends(current_member),
                 db: Any = Depends(db_dep)) -> dict:
    chk = await db.checkouts.find_one({"_id": checkout_id, "member_id": member_id})
    if not chk or chk["razorpay_order_id"] != body.razorpay_order_id:
        raise HTTPException(404, "Checkout not found")
    if not payments.verify_checkout_signature(body.razorpay_order_id, body.razorpay_payment_id,
                                              body.razorpay_signature):
        raise HTTPException(400, "Payment signature does not match")
    res = await svc.confirm_payment(db, body.razorpay_order_id, body.razorpay_payment_id)
    return {"checkout_id": checkout_id, "status": res["status"]}


@shop.get("/orders", summary="My orders")
async def my_orders(member_id: str = Depends(current_member), db: Any = Depends(db_dep)) -> list[dict]:
    cur = db.orders.find({"member_id": member_id, "status": {"$ne": "expired"}}).sort("created_at", -1)
    return [svc.member_view(o) async for o in cur]


@shop.get("/orders/{order_id}", summary="Order tracker")
async def my_order(order_id: str, member_id: str = Depends(current_member), db: Any = Depends(db_dep)) -> dict:
    o = await db.orders.find_one({"_id": order_id, "member_id": member_id})
    if not o:
        raise HTTPException(404, "Order not found")
    return svc.member_view(o)


@hooks.post("/razorpay/webhook", summary="Razorpay webhook (marks orders paid)")
async def razorpay_webhook(request: Request, x_razorpay_signature: str | None = Header(None),
                           x_razorpay_event_id: str | None = Header(None), db: Any = Depends(db_dep)) -> dict:
    body = await request.body()
    if not payments.verify_webhook(body, x_razorpay_signature):
        raise HTTPException(400, "Bad signature")
    event = json.loads(body)
    event_id = x_razorpay_event_id or new_id("whk")
    try:
        await db.webhook_events.insert_one({"_id": new_id("whe"), "event_id": event_id,
                                            "event": event.get("event"), "received_at": utcnow()})
    except DuplicateKeyError:
        return {"status": "duplicate"}

    name = event.get("event")
    if name in ("payment.captured", "order.paid"):
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment.get("order_id")
        if order_id:
            try:
                await svc.confirm_payment(db, order_id, payment.get("id"))
            except HTTPException as e:
                if e.status_code != 404:
                    raise
    # payment.failed: Razorpay lets the member retry on the same order, so we wait for expiry.
    return {"status": "ok"}


# ---- Vendor Orders tab -----------------------------------------------------------

OrderStatus = Literal["placed", "packed", "shipped", "delivered", "cancelled"]


class OrderMove(BaseModel):
    status: Literal["packed", "shipped", "delivered", "cancelled"]
    courier: str | None = Field(default=None, max_length=80)
    tracking_number: str | None = Field(default=None, max_length=80)
    tracking_url: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=500, description="Required when cancelling")


def _vendor_order(o: dict) -> dict:
    d = out(o, hide=("member_id",))
    d["member_label"] = svc.MEMBER_LABELS[o["status"]]
    d["next_statuses"] = sorted(svc.VENDOR_TRANSITIONS.get(o["status"], set()))
    return d


@vendor.get("/orders", summary="Vendor orders")
async def vendor_orders(status: OrderStatus | None = None, page: int = Query(1, ge=1),
                        limit: int = Query(30, ge=1, le=100), me: VendorPrincipal = Depends(current_vendor),
                        db: Any = Depends(db_dep)) -> dict:
    filt: dict = {"vendor_id": me.vendor_id}
    filt["status"] = status if status else {"$in": svc.LIVE_STATUSES + ["cancelled"]}
    total = await db.orders.count_documents(filt)
    cur = db.orders.find(filt).sort("created_at", -1).skip((page - 1) * limit).limit(limit)
    return {"items": [_vendor_order(o) async for o in cur], "total": total, "page": page}


@vendor.get("/orders/{order_id}")
async def vendor_order(order_id: str, me: VendorPrincipal = Depends(current_vendor), db: Any = Depends(db_dep)) -> dict:
    o = await db.orders.find_one({"_id": order_id, "vendor_id": me.vendor_id, "status": {"$nin": ["payment_pending", "expired"]}})
    if not o:
        raise HTTPException(404, "Order not found")
    return _vendor_order(o)


@vendor.patch("/orders/{order_id}", summary="Move the order: Packed, Shipped, Delivered or Cancelled")
async def move_order(order_id: str, body: OrderMove, me: VendorPrincipal = Depends(current_vendor),
                     db: Any = Depends(db_dep)) -> dict:
    o = await db.orders.find_one({"_id": order_id, "vendor_id": me.vendor_id})
    if not o:
        raise HTTPException(404, "Order not found")
    res = await svc.vendor_move(db, o, me.vendor, body.status, body.courier, body.tracking_number,
                                body.tracking_url, body.note)
    return _vendor_order(res)
