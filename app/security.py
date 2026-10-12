import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import Any

import bcrypt
import jwt

from app.config import get_settings
from app.utils import utcnow


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def create_access_token(sub: str, role: str, **claims: Any) -> str:
    s = get_settings()
    now = utcnow()
    payload = {
        "sub": sub,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_expire_minutes)).timestamp()),
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret_token() -> tuple[str, str]:
    """Returns (raw token to send to the user, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, sha256(raw)


def _link_sig(file_id: str, exp: int) -> str:
    key = get_settings().link_signing_secret.encode()
    return hmac.new(key, f"{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_file_url(file_id: str) -> tuple[str, int]:
    s = get_settings()
    exp = int(time.time()) + s.private_link_ttl_seconds
    return f"{s.public_base_url}/files/private/{file_id}?exp={exp}&sig={_link_sig(file_id, exp)}", exp


def verify_file_signature(file_id: str, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_link_sig(file_id, exp), sig)


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
