from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SuppAI Vendor API"
    env: str = "development"  # development | staging | production

    # MongoDB. Transactions need a replica set (docker-compose starts a single-node one).
    mongo_uri: str = "mongodb://localhost:27017/?replicaSet=rs0"
    mongo_db: str = "suppai_vendor"
    mongo_transactions: bool = True

    # Auth
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 720
    # Member tokens are issued by the main SuppAI app; this service only verifies them.
    member_jwt_secret: str = "change-me-member"
    link_signing_secret: str = "change-me-links"
    set_password_ttl_hours: int = 24
    private_link_ttl_seconds: int = 300
    # Return the set-password link to the admin in the accept response (handy in dev).
    expose_invite_links: bool = True

    public_base_url: str = "http://localhost:8000"
    frontend_base_url: str = "https://suppai.in"
    cors_origins: list[str] = ["https://suppai.in", "http://localhost:3000"]

    # Files: "gridfs" in real deployments, "memory" for tests.
    file_store: str = "gridfs"
    max_image_mb: int = 5
    max_document_mb: int = 10

    # Shop search: "text" uses a MongoDB text index; "regex" works without one.
    search_mode: str = "text"

    # Commerce
    default_commission_pct: float = 15.0
    payment_pending_ttl_minutes: int = 30

    # Razorpay. With no key id/secret the service runs in mock mode.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = "change-me-webhook"

    # Email. With no SMTP host, mails are only written to the `outbox` collection.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "SuppAI <no-reply@suppai.in>"

    # Background jobs (payment expiry + payouts) inside the API process.
    run_background_jobs: bool = False
    background_job_interval_seconds: int = 300

    @property
    def razorpay_mock(self) -> bool:
        return not (self.razorpay_key_id and self.razorpay_key_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
