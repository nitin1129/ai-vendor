import io
import os

os.environ.update({
    "FILE_STORE": "memory",
    "SEARCH_MODE": "regex",
    "MONGO_TRANSACTIONS": "false",
    "JWT_SECRET": "test-secret-0123456789abcdef0123456789",
    "MEMBER_JWT_SECRET": "member-secret-0123456789abcdef012345678",
    "RAZORPAY_WEBHOOK_SECRET": "whsec",
    "RAZORPAY_KEY_ID": "",
    "RAZORPAY_KEY_SECRET": "",
})

import jwt  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import db as database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.storage import MemoryStore, set_store  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_EMAIL, ADMIN_PW = "admin@suppai.in", "Admin123!"


@pytest.fixture
async def db():
    client = AsyncMongoMockClient(tz_aware=True)
    mdb = client["test"]
    database.set_database(mdb, None)
    set_store(MemoryStore())
    await database.ensure_indexes(mdb)
    await mdb.admins.insert_one({"_id": "adm_1", "email": ADMIN_EMAIL, "name": "Ops",
                                 "password_hash": hash_password(ADMIN_PW), "active": True,
                                 "created_at": utcnow()})
    yield mdb
    database.set_database(None, None)


@pytest.fixture
async def client(db):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_h(client):
    r = await client.post("/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def member_headers(member_id: str = "mem_1") -> dict:
    token = jwt.encode({"sub": member_id, "role": "member"}, "member-secret-0123456789abcdef012345678", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def png_bytes(color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), color).save(buf, "PNG")
    return buf.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def steps(company="Nutriva Wellness Pvt. Ltd.", email="partners@nutriva.in", cycle="weekly", return_days=7):
    return {
        "company": {
            "legal_name": company, "trade_name": None, "entity_type": "private_limited",
            "registered_address": {"line1": "12 Main Road", "city": "Ranchi", "state": "Jharkhand", "pincode": "834005"},
            "contact_name": "Asha Rao", "official_email": email, "phone": "9876543210",
        },
        "signatory": {"name": "Asha Rao", "designation": "Director", "mobile": "9876543210",
                      "email": email, "pan": "abcde1234f", "aadhaar": "2345 6789 0123"},
        "statutory": {"categories": ["supplements"], "gstin": "20ABCDE1234F1Z5",
                      "fssai_licence_no": "12345678901234"},
        "bank": {"account_holder": company, "account_number": "123456789012", "ifsc": "HDFC0001234",
                 "bank_name": "HDFC Bank", "settlement_cycle": cycle},
        "commercial": {"trade_discount_pct": 10, "delivery_days": 4, "return_window_days": return_days},
        "declarations": {"gmp_certified": True, "declare_information_true": True,
                         "declare_no_banned_substances": True, "agree_to_terms": True},
    }


async def apply(client, **kw) -> tuple[str, dict]:
    r = await client.post("/vendors/applications")
    assert r.status_code == 201
    app_id, token = r.json()["id"], r.json()["draft_token"]
    h = {"X-Draft-Token": token}
    for key, body in steps(**kw).items():
        r = await client.put(f"/vendors/applications/{app_id}/steps/{key}", json=body, headers=h)
        assert r.status_code == 200, r.text
    for doc in ("gst_certificate", "pan_card", "cancelled_cheque", "fssai_licence"):
        r = await client.post(f"/vendors/applications/{app_id}/documents", data={"doc_type": doc},
                              files={"file": (f"{doc}.pdf", PDF, "application/pdf")}, headers=h)
        assert r.status_code == 201, r.text
    return app_id, h


async def onboard(client, admin_h, password="Vendor123", **kw) -> dict:
    """Apply, submit, accept, set password. Returns vendor auth headers + ids."""
    app_id, h = await apply(client, **kw)
    r = await client.post(f"/vendors/applications/{app_id}/submit", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/admin/vendors/applications/{app_id}/accept", json={"commission_pct": 10},
                          headers=admin_h)
    assert r.status_code == 200, r.text
    acc = r.json()
    token = acc["invite_link"].split("token=")[1]
    r = await client.post("/auth/vendor/set-password", json={"token": token, "password": password})
    assert r.status_code == 200, r.text
    return {"headers": {"Authorization": f"Bearer {r.json()['access_token']}"}, **acc, "app_id": app_id}


async def upload_image(client, vh) -> str:
    r = await client.post("/vendor/uploads/images", files={"file": ("p.png", png_bytes(), "image/png")}, headers=vh)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def product_body(img_id: str, **over) -> dict:
    body = {
        "name": "Nutriva Omega-3 Fish Oil 1000mg", "brand": "Nutriva", "category": "supplements",
        "hsn_code": "21069099", "tags": ["omega 3", "heart"],
        "short_description": "High strength omega-3 softgels",
        "description": "Each softgel gives 1000mg fish oil with EPA and DHA for daily heart support.",
        "ingredients": "Fish oil, gelatin, glycerin", "dosage": "1 softgel daily after a meal",
        "mrp": 999, "selling_price": 799, "supply_cost": 560, "gst_pct": 18, "moq": 10,
        "lead_time_days": 3, "images": [img_id], "stock": 20, "discount_pct": 0,
    }
    body.update(over)
    return body


async def listed_product(client, admin_h, vh, **over) -> str:
    img = await upload_image(client, vh)
    r = await client.post("/vendor/products", json=product_body(img, **over), headers=vh)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(f"/admin/catalogue/{pid}/approve", json={}, headers=admin_h)
    assert r.status_code == 200, r.text
    return pid
