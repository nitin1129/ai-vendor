from .conftest import listed_product, onboard, product_body, upload_image


async def test_listing_review_loop_and_shop_visibility(client, admin_h, db):
    v = await onboard(client, admin_h)
    vh = v["headers"]
    img = await upload_image(client, vh)
    r = await client.post("/vendor/products", json=product_body(img), headers=vh)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["status"] == "awaiting_review" and p["shop_label"] == "Hidden"
    assert p["margin"] == 239.0 and p["final_price"] == 799.0
    assert set(p["images"][0]) == {"id", "thumb", "medium", "large"}
    assert (await client.get("/shop/products")).json()["total"] == 0

    pending = (await client.get("/admin/catalogue?status=pending", headers=admin_h)).json()
    assert pending["total"] == 1 and pending["items"][0]["request_type"] == "new"

    r = await client.post(f"/admin/catalogue/{p['id']}/changes", json={"note": "Add the FSSAI number to the label photo"},
                          headers=admin_h)
    assert r.json()["status"] == "changes_requested"
    r = await client.patch(f"/vendor/products/{p['id']}", json={"short_description": "High strength omega-3, FSSAI listed"},
                           headers=vh)
    assert r.json()["status"] == "awaiting_review"

    r = await client.post(f"/admin/catalogue/{p['id']}/approve", json={}, headers=admin_h)
    assert r.json()["status"] == "listed"
    shop = (await client.get("/shop/products")).json()
    assert shop["total"] == 1
    item = shop["items"][0]
    assert item["price"] == 799.0 and "supply_cost" not in item and "margin" not in item

    hist = (await client.get(f"/vendor/products/{p['id']}", headers=vh)).json()["history"]
    assert [e["type"] for e in hist] == ["submitted", "changes_requested", "resubmitted", "approved"]
    assert hist[1]["note"] == "Add the FSSAI number to the label photo"


async def test_edit_on_listed_product_stays_live_until_approved(client, admin_h):
    v = await onboard(client, admin_h)
    vh = v["headers"]
    pid = await listed_product(client, admin_h, vh)

    r = await client.patch(f"/vendor/products/{pid}", json={"selling_price": 749}, headers=vh)
    assert r.json()["status"] == "listed"
    assert r.json()["pending_edit"]["changes"] == {"selling_price": 749.0}
    assert (await client.get(f"/shop/products/{pid}")).json()["price"] == 799.0  # old price still live

    detail = (await client.get(f"/admin/catalogue/{pid}", headers=admin_h)).json()
    assert detail["request_type"] == "edit"
    assert detail["diff"]["selling_price"] == {"from": 799.0, "to": 749.0}
    assert detail["diff"]["final_price"] == {"from": 799.0, "to": 749.0}

    r = await client.post(f"/admin/catalogue/{pid}/approve", json={}, headers=admin_h)
    assert r.json()["pending_edit"] is None
    assert (await client.get(f"/shop/products/{pid}")).json()["price"] == 749.0

    # Rejecting an edit leaves the live product alone.
    await client.patch(f"/vendor/products/{pid}", json={"name": "Totally different product name"}, headers=vh)
    r = await client.post(f"/admin/catalogue/{pid}/reject", json={"note": "Name is misleading"},
                          headers=admin_h)
    assert r.json()["status"] == "listed" and r.json()["name"] == "Nutriva Omega-3 Fish Oil 1000mg"


async def test_stock_and_discount_apply_at_once(client, admin_h):
    v = await onboard(client, admin_h)
    vh = v["headers"]
    pid = await listed_product(client, admin_h, vh)

    r = await client.patch(f"/vendor/products/{pid}/stock", json={"discount_pct": 10}, headers=vh)
    assert r.json()["final_price"] == 719.1 and r.json()["pending_edit"] is None
    r = await client.patch(f"/vendor/products/{pid}/stock", json={"in_stock": False}, headers=vh)
    assert r.json()["shop_label"] == "Shown, cannot be bought"
    item = (await client.get(f"/shop/products/{pid}")).json()
    assert item["in_stock"] is False
    await client.patch(f"/vendor/products/{pid}/stock", json={"in_stock": True}, headers=vh)
    assert (await client.get(f"/shop/products/{pid}")).json()["in_stock"] is True

    r = await client.delete(f"/vendor/products/{pid}", headers=vh)
    assert r.json()["status"] == "removed"
    assert (await client.get(f"/shop/products/{pid}")).status_code == 404


async def test_price_checks_and_gst_from_hsn(client, admin_h):
    v = await onboard(client, admin_h)
    vh = v["headers"]
    img = await upload_image(client, vh)
    r = await client.post("/vendor/products", json=product_body(img, selling_price=1200), headers=vh)
    assert r.status_code == 422 and "at or below MRP" in r.text

    r = await client.put("/admin/hsn-rates/2106", json={"gst_pct": 5, "description": "Food preparations"},
                         headers=admin_h)
    assert r.status_code == 200
    r = await client.post("/vendor/products", json=product_body(img, gst_pct=None), headers=vh)
    assert r.status_code == 201 and r.json()["gst_pct"] == 5.0

    r = await client.post("/vendor/products", json=product_body(img, hsn_code="30049099", gst_pct=None), headers=vh)
    assert r.status_code == 422 and "GST" in r.text


async def test_vendor_isolation_and_image_ownership(client, admin_h):
    a = await onboard(client, admin_h)
    b = await onboard(client, admin_h, company="Herbline Naturals LLP", email="hello@herbline.in")
    pid = await listed_product(client, admin_h, a["headers"])
    assert (await client.get(f"/vendor/products/{pid}", headers=b["headers"])).status_code == 404
    assert (await client.patch(f"/vendor/products/{pid}/stock", json={"stock": 0},
                               headers=b["headers"])).status_code == 404
    img_a = await upload_image(client, a["headers"])
    r = await client.post("/vendor/products", json=product_body(img_a), headers=b["headers"])
    assert r.status_code == 422 and "Unknown image" in r.text


async def test_shop_search_filters_and_sort(client, admin_h):
    v = await onboard(client, admin_h)
    vh = v["headers"]
    await listed_product(client, admin_h, vh)
    await listed_product(client, admin_h, vh, name="Nutriva Whey Protein Chocolate", category="protein_sports_nutrition",
                         mrp=2999, selling_price=2499, supply_cost=1900, tags=["whey"])
    r = (await client.get("/shop/products?q=whey")).json()
    assert r["total"] == 1 and r["items"][0]["category"] == "protein_sports_nutrition"
    r = (await client.get("/shop/products?sort=price_desc")).json()
    assert [i["price"] for i in r["items"]] == [2499.0, 799.0]
    r = (await client.get("/shop/products?category=supplements")).json()
    assert r["total"] == 1
    cats = {c["category"]: c["count"] for c in (await client.get("/shop/categories")).json()}
    assert cats["supplements"] == 1 and cats["protein_sports_nutrition"] == 1
    img = (await client.get("/shop/products")).json()["items"][0]["images"][0]["medium"]
    r = await client.get(img.split("http://localhost:8000")[1])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
