"""Razorpay: create an order, verify signatures, refund. Mock mode when keys are not set."""
import hmac
import secrets

import httpx

from app.config import get_settings
from app.security import hmac_sha256_hex

API = "https://api.razorpay.com/v1"


class PaymentError(RuntimeError):
    pass


async def create_order(amount_paise: int, receipt: str, notes: dict) -> dict:
    s = get_settings()
    if s.razorpay_mock:
        return {"id": f"order_mock_{secrets.token_hex(7)}", "amount": amount_paise, "currency": "INR"}
    async with httpx.AsyncClient(timeout=15, auth=(s.razorpay_key_id, s.razorpay_key_secret)) as c:
        r = await c.post(f"{API}/orders", json={"amount": amount_paise, "currency": "INR",
                                                "receipt": receipt[:40], "notes": notes})
    if r.status_code >= 300:
        raise PaymentError(f"Razorpay order failed: {r.text}")
    return r.json()


async def refund(payment_id: str, amount_paise: int, notes: dict) -> dict:
    s = get_settings()
    if s.razorpay_mock or not payment_id:
        return {"id": f"rfnd_mock_{secrets.token_hex(7)}", "amount": amount_paise, "status": "processed"}
    async with httpx.AsyncClient(timeout=15, auth=(s.razorpay_key_id, s.razorpay_key_secret)) as c:
        r = await c.post(f"{API}/payments/{payment_id}/refund", json={"amount": amount_paise, "notes": notes})
    if r.status_code >= 300:
        raise PaymentError(f"Razorpay refund failed: {r.text}")
    return r.json()


def verify_webhook(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac_sha256_hex(get_settings().razorpay_webhook_secret, body)
    return hmac.compare_digest(expected, signature)


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """The signature Razorpay Checkout hands the browser after payment."""
    s = get_settings()
    secret = s.razorpay_key_secret or s.razorpay_webhook_secret  # mock mode reuses the webhook secret
    expected = hmac_sha256_hex(secret, f"{order_id}|{payment_id}".encode())
    return hmac.compare_digest(expected, signature)
