"""Load HSN -> GST rates used to fill GST on product listings.

    python -m scripts.seed_hsn data/hsn_rates.json
"""
import asyncio
import json
import sys

from app import db as database
from app.utils import utcnow


async def main(path: str) -> None:
    rows = json.load(open(path))["rates"]
    db = database.connect()
    loaded = skipped = 0
    for r in rows:
        if r.get("gst_pct") is None:
            skipped += 1
            continue
        await db.hsn_rates.update_one({"_id": str(r["hsn"])}, {"$set": {
            "gst_pct": float(r["gst_pct"]), "description": r.get("description"), "updated_at": utcnow()}},
            upsert=True)
        loaded += 1
    print(f"loaded {loaded}, skipped {skipped} without a rate")
    database.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "data/hsn_rates.json"))
