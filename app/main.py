import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db as database
from app.config import get_settings
from app.routers import (admin_catalogue, admin_vendors, applications, auth, files, orders, reports,
                         shop, vendor_products)
from app.services.orders import expire_stale
from app.services.payouts import run_payouts
from app.storage import GridFSStore, MemoryStore, set_store

log = logging.getLogger("suppai.vendor")


async def _job_loop() -> None:
    s = get_settings()
    while True:
        try:
            db = database.get_db()
            await expire_stale(db)
            await run_payouts(db)
        except Exception:
            log.exception("Background job failed")
        await asyncio.sleep(s.background_job_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if database._db is None:  # tests plug in their own database
        db = database.connect()
        set_store(GridFSStore(db) if s.file_store == "gridfs" else MemoryStore())
        await database.ensure_indexes(db)
    task = asyncio.create_task(_job_loop()) if s.run_background_jobs else None
    yield
    if task:
        task.cancel()
    database.close()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan,
                  description="Vendors join, list products, get them approved into the SuppAI Shop, and ship orders.")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    for r in (
        applications.router, auth.router,
        admin_vendors.router, admin_catalogue.router, reports.admin,
        vendor_products.router, orders.vendor, reports.vendor,
        shop.router, orders.shop, orders.hooks, files.router,
    ):
        app.include_router(r)

    @app.get("/health", tags=["System"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
