import json
from datetime import timedelta

from app.security import hmac_sha256_hex
from app.utils import utcnow

from .conftest import listed_product, member_headers, onboard

ADDRESS = {"line1": "4 Lake View", "city": "Ranchi", "state": "Jharkhand", "pincode": "834001"}
CONTACT = {"name": "Ravi Kumar", "phone": "9123456780"}


async def _checkout(client, items, member="mem_1"):
    return await client.post("/shop/checkout", json={"items": items, "shipping_address": ADDRESS, "contact": CONTACT},
                             headers=member_headers(member))


async def _pay_webhook(client, rp_order_id, payment_id="pay_test_1", event_id="evt_1"):
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": payment_id, "order_id": rp_order_id, "status": "captured"}}}}).encode()
    return await client.post("/payments/razorpay/webhook", content=body, headers={
        "X-Razorpay-Signature": hmac_sha256_hex("whsec", body), "X-Razorpay-Event-Id": event_id,
        "Content-Type": "application/json"})


async def _stock(client, pid):
    return (await client.get(f"/shop/products/{pid}")).json()["stock"]


async def test_full_order_flow_split_by_vendor(client, admin_h, db):
    a = await onboard(client, admin_h)
    b = await onboard(client, admin_h, company="Herbline Naturals LLP", email="hello@herbline.in")
    pa = await listed_product(client, admin_h, a["headers"])
    pb = await listed_product(client, admin_h, b["headers"], name="Herbline Ashwagandha Tablets",
                              mrp=499, selling_price=399, supply_cost=250)

    r = await _checkout(client, [{"product_id": pa, "qty": 2}, {"product_id": pb, "qty": 1}])
    assert r.status_code == 201, r.text
    chk = r.json()
    assert len(chk["orders"]) == 2 and chk["amount"] == 1997.0 and chk["amount_paise"] == 199700
    assert await _stock(client, pa) == 18  # reserved at checkout

    # Vendor does not see unpaid orders.
    assert (await client.get("/vendor/orders", headers=a["headers"])).json()["total"] == 0

    r = await _pay_webhook(client, chk["razorpay_order_id"])
    assert r.json()["status"] == "ok"
    r = await _pay_webhook(client, chk["razorpay_order_id"])  # same event again
    assert r.json()["status"] == "duplicate"
    assert await db.outbox.count_documents({"kind": "vendor_new_order"}) == 2

    orders = (await client.get("/vendor/orders", headers=a["headers"])).json()["items"]
    assert len(orders) == 1 and orders[0]["status"] == "placed"
    assert {i["product_id"] for i in orders[0]["items"]} == {pa}  # vendor ships only its own items
    assert "member_id" not in orders[0]
    oid = orders[0]["id"]

    # Other vendor can't touch it.
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "packed"}, headers=b["headers"])
    assert r.status_code == 404

    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "shipped"}, headers=a["headers"])
    assert r.status_code == 409  # can't skip packed
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "packed"}, headers=a["headers"])
    assert r.json()["member_label"] == "Packed"
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "shipped"}, headers=a["headers"])
    assert r.status_code == 422  # tracking required
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "shipped", "courier": "Delhivery",
                                                          "tracking_number": "DL123456789"}, headers=a["headers"])
    assert r.json()["shipment"]["tracking_number"] == "DL123456789"

    tracker = (await client.get(f"/shop/orders/{oid}", headers=member_headers())).json()
    assert tracker["label"] == "Shipped"
    assert [s["done"] for s in tracker["tracker"]] == [True, True, True, False]
    assert tracker["shipment"]["courier"] == "Delhivery"
    assert (await client.get(f"/shop/orders/{oid}", headers=member_headers("mem_2"))).status_code == 404

    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "delivered"}, headers=a["headers"])
    assert r.json()["payout_status"] == "pending" and r.json()["return_window_ends_at"]
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "cancelled", "note": "x"}, headers=a["headers"])
    assert r.status_code == 409

    summary = (await client.get("/vendor/reports/summary", headers=a["headers"])).json()
    assert summary["revenue"] == 1598.0 and summary["units_sold"] == 2 and summary["orders"] == 1
    assert summary["top_seller"]["product_id"] == pa
    assert summary["catalogue_health"]["listed"] == 1

    # Payout: after the return window, on the settlement cycle, minus commission (10%).
    later = utcnow() + timedelta(days=8)
    from app.services.payouts import run_payouts
    payouts = await run_payouts(db, now=later)
    assert len(payouts) == 1
    p = payouts[0]
    assert p["vendor_id"] == a["vendor_id"] and p["gross"] == 1598.0 and p["commission"] == 159.8 and p["net"] == 1438.2
    assert p["bank"]["account_number"].endswith("9012") and "1234" not in p["bank"]["account_number"][:8]
    assert await run_payouts(db, now=later) == []  # not paid twice
    r = await client.post(f"/admin/payouts/{p['_id']}/mark-paid", json={"utr": "HDFCN52026101100"}, headers=admin_h)
    assert r.json()["status"] == "paid"
    o = await db.orders.find_one({"_id": oid})
    assert o["payout_status"] == "paid"


async def test_cancel_refunds_and_restores_stock(client, admin_h, db):
    a = await onboard(client, admin_h)
    pid = await listed_product(client, admin_h, a["headers"])
    chk = (await _checkout(client, [{"product_id": pid, "qty": 3}])).json()
    await _pay_webhook(client, chk["razorpay_order_id"])
    oid = chk["orders"][0]["id"]
    assert await _stock(client, pid) == 17
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "cancelled"}, headers=a["headers"])
    assert r.status_code == 422
    r = await client.patch(f"/vendor/orders/{oid}", json={"status": "cancelled", "note": "Batch recalled"},
                           headers=a["headers"])
    assert r.json()["status"] == "cancelled" and r.json()["refund_status"] == "processed"
    assert await _stock(client, pid) == 20
    assert (await client.get(f"/shop/orders/{oid}", headers=member_headers())).json()["label"] == "Cancelled"


async def test_unpaid_checkout_expires_and_releases_stock(client, admin_h, db):
    a = await onboard(client, admin_h)
    pid = await listed_product(client, admin_h, a["headers"], stock=2)
    chk = (await _checkout(client, [{"product_id": pid, "qty": 2}])).json()
    assert await _stock(client, pid) == 0
    r = await _checkout(client, [{"product_id": pid, "qty": 1}], member="mem_2")
    assert r.status_code == 409  # sold out while the first payment is pending

    await db.checkouts.update_one({"_id": chk["checkout_id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    r = await client.post("/admin/jobs/run", headers=admin_h)
    assert r.json()["expired_checkouts"] == 1
    assert await _stock(client, pid) == 2
    assert (await db.orders.find_one({"checkout_id": chk["checkout_id"]}))["status"] == "expired"

    # A payment that lands after expiry is refunded, not turned into an order.
    await _pay_webhook(client, chk["razorpay_order_id"], event_id="evt_late")
    c = await db.checkouts.find_one({"_id": chk["checkout_id"]})
    assert c["status"] == "refunded_late"
    assert await _stock(client, pid) == 2


async def test_checkout_rejects_unavailable_and_bad_signatures(client, admin_h):
    a = await onboard(client, admin_h)
    pid = await listed_product(client, admin_h, a["headers"], stock=1)
    r = await _checkout(client, [{"product_id": pid, "qty": 5}])
    assert r.status_code == 409 and "only 1 left" in r.text
    r = await _checkout(client, [{"product_id": "prd_missing", "qty": 1}])
    assert r.status_code == 409
    r = await client.post("/shop/checkout", json={"items": [{"product_id": pid, "qty": 1}],
                                                  "shipping_address": ADDRESS, "contact": CONTACT})
    assert r.status_code == 401

    chk = (await _checkout(client, [{"product_id": pid, "qty": 1}])).json()
    r = await client.post("/payments/razorpay/webhook", content=b'{"event":"payment.captured"}',
                          headers={"X-Razorpay-Signature": "bad"})
    assert r.status_code == 400

    # Browser verify path (Razorpay Checkout handler)
    sig = hmac_sha256_hex("whsec", f"{chk['razorpay_order_id']}|pay_browser_1".encode())
    r = await client.post(f"/shop/checkout/{chk['checkout_id']}/verify", headers=member_headers(), json={
        "razorpay_order_id": chk["razorpay_order_id"], "razorpay_payment_id": "pay_browser_1",
        "razorpay_signature": "0" * 64})
    assert r.status_code == 400
    r = await client.post(f"/shop/checkout/{chk['checkout_id']}/verify", headers=member_headers(), json={
        "razorpay_order_id": chk["razorpay_order_id"], "razorpay_payment_id": "pay_browser_1",
        "razorpay_signature": sig})
    assert r.json()["status"] == "paid"
    mine = (await client.get("/shop/orders", headers=member_headers())).json()
    assert mine[0]["label"] == "Order placed"
