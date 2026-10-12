"""Public vendor application at /join/vendor (seven steps, saved as a draft)."""
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import ValidationError

from app.config import get_settings
from app.deps import db_dep
from app.schemas.applications import STEP_MODELS, STEP_ORDER, DocType, SignatoryStep, required_documents
from app.security import new_secret_token, sha256
from app.services.applications import missing_for_submit, set_status
from app.services.common import send_email
from app.storage import get_store
from app.utils import new_id, next_seq, out, utcnow

router = APIRouter(prefix="/vendors/applications", tags=["Vendor application"])

ALLOWED_DOC_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


def _public(app: dict) -> dict:
    d = out(app, hide=("draft_token_hash", "admin_note"))
    categories = app.get("steps", {}).get("statutory", {}).get("categories", [])
    d["required_documents"] = sorted(required_documents(categories))
    d["steps_done"] = [k for k in STEP_ORDER if k in app.get("steps", {}) or
                       (k == "documents" and app.get("documents"))]
    return d


async def _load_draft(db: Any, app_id: str, token: str | None) -> dict:
    app = await db.vendor_applications.find_one({"_id": app_id})
    if not app or not token or app.get("draft_token_hash") != sha256(token):
        raise HTTPException(404, "Application not found")
    return app


def _require_draft(app: dict) -> None:
    if app["status"] != "draft":
        raise HTTPException(409, "This application has already been submitted")


@router.post("", status_code=201, summary="Start an application (returns a draft token)")
async def start_application(db: Any = Depends(db_dep)) -> dict:
    raw, token_hash = new_secret_token()
    now = utcnow()
    app = {"_id": new_id("app"), "status": "draft", "reference": None, "draft_token_hash": token_hash,
           "steps": {}, "documents": [], "status_history": [], "created_at": now, "updated_at": now}
    await db.vendor_applications.insert_one(app)
    return {"id": app["_id"], "draft_token": raw, "steps": STEP_ORDER,
            "note": "Send the draft token as the X-Draft-Token header on later calls"}


@router.get("/{app_id}", summary="Read your draft or its status")
async def get_application(app_id: str, x_draft_token: str | None = Header(None), db: Any = Depends(db_dep)) -> dict:
    return _public(await _load_draft(db, app_id, x_draft_token))


@router.put("/{app_id}/steps/{step}", summary="Save one step of the form")
async def save_step(app_id: str, step: str, body: dict, x_draft_token: str | None = Header(None),
                    db: Any = Depends(db_dep)) -> dict:
    if step not in STEP_MODELS:
        raise HTTPException(404, f"Unknown step. Use one of: {', '.join(STEP_MODELS)} (documents are uploaded)")
    app = await _load_draft(db, app_id, x_draft_token)
    _require_draft(app)
    try:
        model = STEP_MODELS[step].model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))
    data = model.to_store() if isinstance(model, SignatoryStep) else model.model_dump(mode="json")
    if step == "company":
        data["official_email"] = data["official_email"].lower()
    await db.vendor_applications.update_one(
        {"_id": app_id}, {"$set": {f"steps.{step}": data, "updated_at": utcnow()}})
    return _public(await db.vendor_applications.find_one({"_id": app_id}))


@router.post("/{app_id}/documents", status_code=201, summary="Upload a mandatory document (step 6)")
async def upload_document(app_id: str, doc_type: DocType = Form(...), file: UploadFile = File(...),
                          x_draft_token: str | None = Header(None), db: Any = Depends(db_dep)) -> dict:
    app = await _load_draft(db, app_id, x_draft_token)
    _require_draft(app)
    if file.content_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(415, "Upload a PDF, JPG, PNG or WebP")
    data = await file.read()
    if len(data) > get_settings().max_document_mb * 1024 * 1024:
        raise HTTPException(413, "File is too large")
    if not data:
        raise HTTPException(422, "File is empty")
    file_id = await get_store().put(data, file.filename or doc_type, file.content_type,
                                    {"kind": "application_document", "private": True, "application_id": app_id})
    doc = {"id": new_id("doc"), "doc_type": doc_type, "file_id": file_id, "filename": file.filename,
           "content_type": file.content_type, "size": len(data), "uploaded_at": utcnow()}
    # One file per type (except "other"): a re-upload replaces the old one.
    old = [d for d in app.get("documents", []) if d["doc_type"] == doc_type and doc_type != "other"]
    if old:
        for d in old:
            await get_store().delete(d["file_id"])
        await db.vendor_applications.update_one(
            {"_id": app_id}, {"$pull": {"documents": {"id": {"$in": [d["id"] for d in old]}}}})
    await db.vendor_applications.update_one(
        {"_id": app_id}, {"$push": {"documents": doc}, "$set": {"updated_at": utcnow()}})
    return {k: v for k, v in doc.items() if k != "file_id"}


@router.delete("/{app_id}/documents/{doc_id}", status_code=204)
async def delete_document(app_id: str, doc_id: str, x_draft_token: str | None = Header(None),
                          db: Any = Depends(db_dep)) -> None:
    app = await _load_draft(db, app_id, x_draft_token)
    _require_draft(app)
    doc = next((d for d in app.get("documents", []) if d["id"] == doc_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    await get_store().delete(doc["file_id"])
    await db.vendor_applications.update_one({"_id": app_id}, {"$pull": {"documents": {"id": doc_id}}})


@router.post("/{app_id}/submit", summary="Submit the application for review")
async def submit_application(app_id: str, x_draft_token: str | None = Header(None),
                             db: Any = Depends(db_dep)) -> dict:
    app = await _load_draft(db, app_id, x_draft_token)
    _require_draft(app)
    problems = missing_for_submit(app)
    if problems:
        raise HTTPException(422, {"message": "The application is incomplete", "problems": problems})
    reference = f"SA-VEN-{await next_seq(db, 'vendor_application', start=1000)}"
    now = utcnow()
    await set_status(db, app, "submitted", None, None, reference=reference, submitted_at=now)
    company = app["steps"]["company"]
    await send_email(
        db, company["official_email"], f"SuppAI vendor application received — {reference}",
        f"Hello {company['contact_name']},\n\nWe have received your application ({reference}). "
        "Our team will review it and email you the outcome.\n\n— Team SuppAI",
        kind="application_received",
    )
    return {"id": app_id, "reference": reference, "status": "submitted", "submitted_at": now}
