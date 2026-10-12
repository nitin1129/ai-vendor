from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.deps import current_vendor, db_dep, VendorPrincipal
from app.security import create_access_token, hash_password, sha256, verify_password
from app.services.applications import reissue_login
from app.utils import as_utc, utcnow

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=3, description="Username or official email")
    password: str = Field(min_length=1)


class SetPasswordIn(BaseModel):
    token: str = Field(min_length=10)
    password: str = Field(min_length=8, max_length=72)


class ForgotIn(BaseModel):
    email: EmailStr


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


def _strong(pw: str) -> None:
    if not (any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw)):
        raise HTTPException(422, "Use at least 8 characters with letters and numbers")


async def _vendor_token(db: Any, user: dict) -> dict:
    vendor = await db.vendors.find_one({"_id": user["vendor_id"]})
    if not vendor or vendor["status"] != "active":
        raise HTTPException(403, "Vendor account is suspended")
    token = create_access_token(user["_id"], "vendor", vendor_id=user["vendor_id"])
    return {"access_token": token, "token_type": "bearer",
            "vendor": {"id": vendor["_id"], "name": vendor["display_name"], "username": user["username"]}}


@router.post("/vendor/login", summary="Vendor sign in")
async def vendor_login(body: LoginIn, db: Any = Depends(db_dep)) -> dict:
    ident = body.username.strip().lower()
    user = await db.vendor_users.find_one({"$or": [{"username": ident}, {"email": ident}]})
    if not user or user["status"] != "active" or not verify_password(body.password, user.get("password_hash")):
        raise HTTPException(401, "Wrong username or password")
    await db.vendor_users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}})
    return await _vendor_token(db, user)


@router.post("/vendor/set-password", summary="First login, from the invite link")
async def vendor_set_password(body: SetPasswordIn, db: Any = Depends(db_dep)) -> dict:
    _strong(body.password)
    user = await db.vendor_users.find_one({"setup.token_hash": sha256(body.token)})
    if not user or as_utc(user["setup"]["expires_at"]) < utcnow():
        raise HTTPException(400, "This link is invalid or has expired. Ask SuppAI for a new one.")
    await db.vendor_users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(body.password), "status": "active",
                  "password_changed_at": utcnow()},
         "$unset": {"setup": ""}},
    )
    return await _vendor_token(db, user)


@router.post("/vendor/forgot-password", summary="Mail a reset link (always answers 200)")
async def vendor_forgot(body: ForgotIn, db: Any = Depends(db_dep)) -> dict:
    user = await db.vendor_users.find_one({"email": body.email.lower()})
    if user:
        await reissue_login(db, user["vendor_id"], admin=None)
    return {"message": "If that email has a vendor account, a reset link is on its way."}


@router.post("/vendor/change-password")
async def vendor_change_password(body: ChangePasswordIn, me: VendorPrincipal = Depends(current_vendor),
                                 db: Any = Depends(db_dep)) -> dict:
    _strong(body.new_password)
    user = await db.vendor_users.find_one({"_id": me.user_id})
    if not user or not verify_password(body.current_password, user.get("password_hash")):
        raise HTTPException(401, "Current password is wrong")
    await db.vendor_users.update_one({"_id": user["_id"]}, {"$set": {
        "password_hash": hash_password(body.new_password), "password_changed_at": utcnow()}})
    return {"message": "Password changed"}


class AdminLoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/admin/login", summary="Admin sign in")
async def admin_login(body: AdminLoginIn, db: Any = Depends(db_dep)) -> dict:
    admin = await db.admins.find_one({"email": body.email.lower(), "active": True})
    if not admin or not verify_password(body.password, admin.get("password_hash")):
        raise HTTPException(401, "Wrong email or password")
    return {"access_token": create_access_token(admin["_id"], "admin"), "token_type": "bearer",
            "admin": {"id": admin["_id"], "name": admin.get("name"), "email": admin["email"]}}
