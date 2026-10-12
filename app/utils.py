import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

from pymongo import ReturnDocument


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def out(doc: dict | None, hide: Iterable[str] = ()) -> dict | None:
    """Mongo document -> API dict: `_id` becomes `id`, listed fields are dropped."""
    if doc is None:
        return None
    d = {k: v for k, v in doc.items() if k not in hide and k != "_id"}
    return {"id": doc.get("_id"), **d}


async def next_seq(db: Any, name: str, start: int = 1000) -> int:
    doc = await db.counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return start + doc["seq"]


_COMPANY_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "opc", "inc", "co", "company",
    "corp", "corporation", "enterprises", "india", "the",
}


def username_base(company_name: str) -> str:
    """'Nutriva Wellness Pvt. Ltd.' -> 'nutriva.wellness'"""
    words = re.findall(r"[a-z0-9]+", company_name.lower())
    kept = [w for w in words if w not in _COMPANY_SUFFIXES] or words or ["vendor"]
    return ".".join(kept[:4])


def mask(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return value
    return "•" * max(len(value) - keep, 0) + value[-keep:]


def money(x: float) -> float:
    return round(float(x) + 1e-9, 2)
