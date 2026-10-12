"""The seven steps of the vendor application at /join/vendor."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

Category = Literal[
    "supplements",
    "vitamins_minerals",
    "protein_sports_nutrition",
    "ayurvedic_herbal",
    "medicines_otc",
    "medical_devices",
    "diabetic_care",
    "healthy_foods",
    "personal_care",
]
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]

# Categories that need a drug licence / FSSAI licence on file.
DRUG_CATEGORIES = {"medicines_otc", "ayurvedic_herbal"}
FOOD_CATEGORIES = {"supplements", "vitamins_minerals", "protein_sports_nutrition", "diabetic_care", "healthy_foods"}

DocType = Literal[
    "gst_certificate",
    "pan_card",
    "cancelled_cheque",
    "drug_licence",
    "fssai_licence",
    "msme_certificate",
    "iec_certificate",
    "gmp_certificate",
    "iso_certificate",
    "trademark_certificate",
    "other",
]

PAN_RE = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
GSTIN_RE = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
IFSC_RE = r"^[A-Z]{4}0[A-Z0-9]{6}$"
PHONE_RE = r"^(\+91)?[6-9][0-9]{9}$"


def _upper(v: str | None) -> str | None:
    return v.strip().upper() if isinstance(v, str) else v


class Address(BaseModel):
    line1: str = Field(min_length=3, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    pincode: str = Field(pattern=r"^[1-9][0-9]{5}$")


class CompanyStep(BaseModel):
    """Step 1 — Vendor / company details."""
    legal_name: str = Field(min_length=2, max_length=200)
    trade_name: str | None = Field(default=None, max_length=200)
    entity_type: Literal["proprietorship", "partnership", "llp", "private_limited", "public_limited", "opc"]
    registered_address: Address
    warehouse_address: Address | None = None
    contact_name: str = Field(min_length=2, max_length=120)
    official_email: EmailStr
    phone: str = Field(pattern=PHONE_RE)
    website: str | None = Field(default=None, max_length=200)


class SignatoryStep(BaseModel):
    """Step 2 — Authorised signatory. Only the last 4 Aadhaar digits are kept."""
    name: str = Field(min_length=2, max_length=120)
    designation: str = Field(min_length=2, max_length=80)
    mobile: str = Field(pattern=PHONE_RE)
    email: EmailStr
    pan: str = Field(pattern=PAN_RE)
    aadhaar: str = Field(pattern=r"^[2-9][0-9]{11}$", description="12 digits; stored masked")

    _up = field_validator("pan", mode="before")(_upper)

    @field_validator("aadhaar", mode="before")
    @classmethod
    def _digits(cls, v: str) -> str:
        return "".join(ch for ch in str(v) if ch.isdigit())

    def to_store(self) -> dict:
        d = self.model_dump(mode="json")
        d["aadhaar_last4"] = d.pop("aadhaar")[-4:]
        return d


class StatutoryStep(BaseModel):
    """Step 3 — Product categories & statutory registrations."""
    categories: list[Category] = Field(min_length=1)
    gstin: str = Field(pattern=GSTIN_RE)
    drug_licence_no: str | None = Field(default=None, max_length=60)
    drug_licence_expiry: date | None = None
    fssai_licence_no: str | None = Field(default=None, pattern=r"^[0-9]{14}$")
    iec: str | None = Field(default=None, pattern=r"^[A-Z0-9]{10}$")
    msme_udyam: str | None = Field(default=None, pattern=r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")

    _up = field_validator("gstin", "iec", "msme_udyam", mode="before")(_upper)

    @model_validator(mode="after")
    def _licences(self):
        cats = set(self.categories)
        if cats & DRUG_CATEGORIES and not self.drug_licence_no:
            raise ValueError("Drug licence number is required for medicines / ayurvedic products")
        if self.drug_licence_no and self.drug_licence_expiry and self.drug_licence_expiry <= date.today():
            raise ValueError("Drug licence has expired")
        if cats & FOOD_CATEGORIES and not self.fssai_licence_no:
            raise ValueError("FSSAI licence number is required for supplements and food products")
        return self


class BankStep(BaseModel):
    """Step 4 — Bank & settlement."""
    account_holder: str = Field(min_length=2, max_length=120)
    account_number: str = Field(pattern=r"^[0-9]{9,18}$")
    ifsc: str = Field(pattern=IFSC_RE)
    bank_name: str = Field(min_length=2, max_length=120)
    branch: str | None = Field(default=None, max_length=120)
    settlement_cycle: Literal["weekly", "fortnightly", "monthly"]

    _up = field_validator("ifsc", mode="before")(_upper)


class CommercialStep(BaseModel):
    """Step 5 — Commercial terms."""
    trade_discount_pct: float = Field(ge=0, le=90)
    delivery_days: int = Field(ge=1, le=60, description="Days from order to delivery")
    return_window_days: int = Field(ge=0, le=30)
    min_order_value: float | None = Field(default=None, ge=0)


class DeclarationsStep(BaseModel):
    """Step 7 — Quality, compliance & declarations."""
    gmp_certified: bool = False
    iso_certified: bool = False
    third_party_lab_tested: bool = False
    quality_notes: str | None = Field(default=None, max_length=2000)
    declare_information_true: bool
    declare_no_banned_substances: bool
    agree_to_terms: bool

    @model_validator(mode="after")
    def _all_declared(self):
        if not (self.declare_information_true and self.declare_no_banned_substances and self.agree_to_terms):
            raise ValueError("All three declarations must be accepted")
        return self


# Step 6 (mandatory documents) is filled through the upload endpoint.
STEP_ORDER = ["company", "signatory", "statutory", "bank", "commercial", "documents", "declarations"]
STEP_MODELS: dict[str, type[BaseModel]] = {
    "company": CompanyStep,
    "signatory": SignatoryStep,
    "statutory": StatutoryStep,
    "bank": BankStep,
    "commercial": CommercialStep,
    "declarations": DeclarationsStep,
}


def required_documents(categories: list[str]) -> set[str]:
    req = {"gst_certificate", "pan_card", "cancelled_cheque"}
    cats = set(categories)
    if cats & DRUG_CATEGORIES:
        req.add("drug_licence")
    if cats & FOOD_CATEGORIES:
        req.add("fssai_licence")
    return req


class AdminNote(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class AcceptIn(BaseModel):
    commission_pct: float | None = Field(default=None, ge=0, le=60)
    note: str | None = Field(default=None, max_length=2000)
