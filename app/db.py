from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, TEXT

from app.config import get_settings

_client: Any = None
_db: Any = None


def connect() -> Any:
    global _client, _db
    s = get_settings()
    _client = AsyncIOMotorClient(s.mongo_uri, tz_aware=True)
    _db = _client[s.mongo_db]
    return _db


def set_database(db: Any, client: Any = None) -> None:
    """Used by tests to plug in a mock database."""
    global _client, _db
    _db, _client = db, client


def get_db() -> Any:
    if _db is None:
        raise RuntimeError("Database not connected")
    return _db


def close() -> None:
    if _client is not None and hasattr(_client, "close"):
        _client.close()


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """Yields a session inside a transaction, or None when transactions are off.

    Callers pass the session through `sess(session)` so the same code runs either way.
    """
    if not get_settings().mongo_transactions or _client is None:
        yield None
        return
    async with await _client.start_session() as session:
        async with session.start_transaction():
            yield session


def sess(session: Any) -> dict:
    return {"session": session} if session is not None else {}


async def ensure_indexes(db: Any) -> None:
    s = get_settings()
    await db.vendor_applications.create_index([("status", ASCENDING), ("submitted_at", DESCENDING)])
    # Drafts have reference=None, so uniqueness applies only to real references.
    await db.vendor_applications.create_index(
        "reference", unique=True, partialFilterExpression={"reference": {"$type": "string"}})
    await db.vendors.create_index("application_id", unique=True)
    await db.vendor_users.create_index("username", unique=True)
    await db.vendor_users.create_index("email", unique=True)
    await db.vendor_users.create_index("setup.token_hash", sparse=True)
    await db.admins.create_index("email", unique=True)
    await db.products.create_index([("status", ASCENDING), ("category", ASCENDING)])
    await db.products.create_index([("vendor_id", ASCENDING), ("status", ASCENDING)])
    await db.products.create_index("pending_edit.status", sparse=True)
    if s.search_mode == "text":
        await db.products.create_index(
            [("name", TEXT), ("brand", TEXT), ("category", TEXT), ("tags", TEXT)],
            weights={"name": 10, "brand": 5, "tags": 3, "category": 1},
            name="product_search",
        )
    await db.product_images.create_index("vendor_id")
    await db.listing_events.create_index([("product_id", ASCENDING), ("at", ASCENDING)])
    await db.orders.create_index([("vendor_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    await db.orders.create_index([("member_id", ASCENDING), ("created_at", DESCENDING)])
    await db.orders.create_index("checkout_id")
    await db.orders.create_index([("status", ASCENDING), ("return_window_ends_at", ASCENDING), ("payout_id", ASCENDING)])
    await db.checkouts.create_index("razorpay_order_id", unique=True)
    await db.checkouts.create_index([("status", ASCENDING), ("expires_at", ASCENDING)])
    await db.shipments.create_index("order_id")
    await db.payouts.create_index([("vendor_id", ASCENDING), ("created_at", DESCENDING)])
    await db.audit_logs.create_index([("at", DESCENDING)])
    await db.webhook_events.create_index("event_id", unique=True)
