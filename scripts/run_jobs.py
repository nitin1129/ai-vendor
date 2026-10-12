"""Expire unpaid checkouts and create due vendor payouts. Run from cron every 5 minutes
if RUN_BACKGROUND_JOBS is off:

    */5 * * * *  cd /srv && python -m scripts.run_jobs
"""
import asyncio

from app import db as database
from app.services.orders import expire_stale
from app.services.payouts import run_payouts


async def main() -> None:
    db = database.connect()
    expired = await expire_stale(db)
    payouts = await run_payouts(db)
    print(f"expired checkouts: {expired}, payouts created: {len(payouts)}")
    database.close()


if __name__ == "__main__":
    asyncio.run(main())
