from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.db import get_db
from app.security import decode_token

bearer = HTTPBearer(auto_error=False)


def db_dep() -> Any:
    return get_db()


def _claims(creds: HTTPAuthorizationCredentials | None, secret: str, role: str) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        claims = decode_token(creds.credentials, secret)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if claims.get("role") != role:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong account type")
    return claims


@dataclass
class VendorPrincipal:
    user_id: str
    vendor_id: str
    vendor: dict


async def current_vendor(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Any = Depends(db_dep)
) -> VendorPrincipal:
    claims = _claims(creds, get_settings().jwt_secret, "vendor")
    # vendor_id always comes from the token, never from the request.
    vendor = await db.vendors.find_one({"_id": claims.get("vendor_id")})
    if not vendor:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Vendor not found")
    if vendor.get("status") != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vendor account is suspended")
    return VendorPrincipal(user_id=claims["sub"], vendor_id=vendor["_id"], vendor=vendor)


async def current_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Any = Depends(db_dep)
) -> dict:
    claims = _claims(creds, get_settings().jwt_secret, "admin")
    admin = await db.admins.find_one({"_id": claims["sub"], "active": True})
    if not admin:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin not found")
    return admin


async def current_member(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    """Member tokens come from the main SuppAI app (shared secret, role=member)."""
    claims = _claims(creds, get_settings().member_jwt_secret, "member")
    return str(claims["sub"])
