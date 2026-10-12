from app.security import signed_file_url

from .conftest import PDF, apply, onboard, steps


async def test_application_draft_submit_and_reference(client, db):
    app_id, h = await apply(client)
    r = await client.get(f"/vendors/applications/{app_id}", headers=h)
    body = r.json()
    assert body["status"] == "draft"
    assert body["steps"]["signatory"]["aadhaar_last4"] == "0123"
    assert "aadhaar" not in body["steps"]["signatory"]
    assert body["steps"]["signatory"]["pan"] == "ABCDE1234F"
    assert set(body["required_documents"]) == {"gst_certificate", "pan_card", "cancelled_cheque", "fssai_licence"}

    r = await client.post(f"/vendors/applications/{app_id}/submit", headers=h)
    assert r.status_code == 200
    assert r.json()["reference"] == "SA-VEN-1001"
    # A submitted application is locked.
    r = await client.put(f"/vendors/applications/{app_id}/steps/commercial",
                         json=steps()["commercial"], headers=h)
    assert r.status_code == 409
    assert await db.outbox.count_documents({"kind": "application_received"}) == 1


async def test_incomplete_application_lists_problems(client):
    r = await client.post("/vendors/applications")
    app_id, h = r.json()["id"], {"X-Draft-Token": r.json()["draft_token"]}
    await client.put(f"/vendors/applications/{app_id}/steps/company", json=steps()["company"], headers=h)
    r = await client.post(f"/vendors/applications/{app_id}/submit", headers=h)
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"]
    assert any("signatory" in p for p in problems)
    assert any("gst_certificate" in p for p in problems)


async def test_step_validation_and_draft_token(client):
    r = await client.post("/vendors/applications")
    app_id, h = r.json()["id"], {"X-Draft-Token": r.json()["draft_token"]}
    bad = {**steps()["statutory"], "gstin": "NOT-A-GSTIN"}
    r = await client.put(f"/vendors/applications/{app_id}/steps/statutory", json=bad, headers=h)
    assert r.status_code == 422
    medicines = {**steps()["statutory"], "categories": ["medicines_otc"]}
    r = await client.put(f"/vendors/applications/{app_id}/steps/statutory", json=medicines, headers=h)
    assert r.status_code == 422 and "Drug licence" in r.text
    r = await client.get(f"/vendors/applications/{app_id}", headers={"X-Draft-Token": "wrong-token-xx"})
    assert r.status_code == 404
    r = await client.post(f"/vendors/applications/{app_id}/documents", data={"doc_type": "pan_card"},
                          files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=h)
    assert r.status_code == 415


async def test_admin_review_accept_and_login(client, admin_h, db):
    app_id, h = await apply(client)
    await client.post(f"/vendors/applications/{app_id}/submit", headers=h)

    r = await client.get("/admin/vendors/applications?status=submitted", headers=admin_h)
    assert r.json()["total"] == 1 and r.json()["items"][0]["legal_name"] == "Nutriva Wellness Pvt. Ltd."
    r = await client.post(f"/admin/vendors/applications/{app_id}/review", headers=admin_h)
    assert r.json()["status"] == "reviewing"
    r = await client.post(f"/admin/vendors/applications/{app_id}/hold", json={"note": "Upload a clearer PAN"},
                          headers=admin_h)
    assert r.json()["status"] == "on_hold"

    detail = (await client.get(f"/admin/vendors/applications/{app_id}", headers=admin_h)).json()
    doc = detail["documents"][0]
    assert "file_id" not in doc
    link = (await client.get(f"/admin/vendors/applications/{app_id}/documents/{doc['id']}/link",
                             headers=admin_h)).json()
    path = link["url"].split("http://localhost:8000")[1]
    r = await client.get(path)
    assert r.status_code == 200 and r.content == PDF
    r = await client.get(path.replace("sig=", "sig=0"))
    assert r.status_code == 403

    r = await client.post(f"/admin/vendors/applications/{app_id}/accept", json={}, headers=admin_h)
    assert r.status_code == 200, r.text
    acc = r.json()
    assert acc["username"] == "nutriva.wellness.vendor"
    assert acc["email"] == "partners@nutriva.in"
    assert acc["commission_pct"] == 15.0
    user = await db.vendor_users.find_one({"username": acc["username"]})
    assert user["password_hash"] is None and user["status"] == "invited"
    assert await db.outbox.count_documents({"kind": "vendor_invite", "to": "partners@nutriva.in"}) == 1

    # Can't accept twice, can't log in before setting a password.
    r = await client.post(f"/admin/vendors/applications/{app_id}/accept", json={}, headers=admin_h)
    assert r.status_code == 409
    r = await client.post("/auth/vendor/login", json={"username": acc["username"], "password": "x"})
    assert r.status_code == 401

    token = acc["invite_link"].split("token=")[1]
    r = await client.post("/auth/vendor/set-password", json={"token": token, "password": "short"})
    assert r.status_code == 422
    r = await client.post("/auth/vendor/set-password", json={"token": token, "password": "Lumen5634"})
    assert r.status_code == 200
    stored = await db.vendor_users.find_one({"_id": user["_id"]})
    assert stored["password_hash"].startswith("$2") and "Lumen5634" not in str(stored)
    # Link is single-use.
    r = await client.post("/auth/vendor/set-password", json={"token": token, "password": "Lumen5634"})
    assert r.status_code == 400
    for ident in (acc["username"], "PARTNERS@nutriva.in"):
        r = await client.post("/auth/vendor/login", json={"username": ident, "password": "Lumen5634"})
        assert r.status_code == 200, ident

    logs = (await client.get("/admin/audit-logs", headers=admin_h)).json()
    assert {"application.accept", "application.hold", "application.view_document"} <= {l["action"] for l in logs}


async def test_reissue_login_and_suspend(client, admin_h):
    v = await onboard(client, admin_h)
    r = await client.post(f"/admin/vendors/{v['vendor_id']}/reissue-login", headers=admin_h)
    assert r.status_code == 200
    r = await client.post("/auth/vendor/login", json={"username": v["username"], "password": "Vendor123"})
    assert r.status_code == 401  # old password no longer works

    v2 = await onboard(client, admin_h, company="Herbline Naturals LLP", email="hello@herbline.in")
    assert v2["username"] == "herbline.naturals.vendor"
    r = await client.patch(f"/admin/vendors/{v2['vendor_id']}", json={"status": "suspended"}, headers=admin_h)
    assert r.status_code == 200
    r = await client.get("/vendor/products", headers=v2["headers"])
    assert r.status_code == 403


async def test_duplicate_company_gets_unique_username(client, admin_h):
    a = await onboard(client, admin_h)
    b = await onboard(client, admin_h, email="other@nutriva.in")
    assert a["username"] == "nutriva.wellness.vendor"
    assert b["username"] == "nutriva.wellness2.vendor"


async def test_role_separation(client, admin_h):
    v = await onboard(client, admin_h)
    r = await client.get("/admin/vendors/applications", headers=v["headers"])
    assert r.status_code == 403
    r = await client.get("/vendor/products", headers=admin_h)
    assert r.status_code == 403
    r = await client.get("/vendor/products")
    assert r.status_code == 401
    _, exp = signed_file_url("x")
    assert exp > 0
