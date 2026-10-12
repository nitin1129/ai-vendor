from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.applications import Category

# Fields whose change on a live product needs admin review again.
CONTENT_FIELDS = (
    "name", "brand", "category", "hsn_code", "tags", "short_description", "description",
    "ingredients", "dosage", "mrp", "selling_price", "supply_cost", "gst_pct", "moq",
    "lead_time_days", "images",
)


class ProductBase(BaseModel):
    name: str = Field(min_length=3, max_length=150)
    brand: str = Field(min_length=1, max_length=80)
    category: Category
    hsn_code: str = Field(pattern=r"^[0-9]{4}([0-9]{2}){0,2}$", description="4, 6 or 8 digits")
    tags: list[str] = Field(default_factory=list, max_length=20)
    short_description: str = Field(min_length=10, max_length=300)
    description: str = Field(min_length=20, max_length=5000)
    ingredients: str = Field(min_length=3, max_length=3000)
    dosage: str = Field(min_length=3, max_length=1000)
    mrp: float = Field(gt=0, le=1_000_000)
    selling_price: float = Field(gt=0, le=1_000_000)
    supply_cost: float = Field(ge=0, le=1_000_000)
    gst_pct: float | None = Field(default=None, ge=0, le=40, description="Filled from the HSN code when known")
    moq: int = Field(default=1, ge=1, le=100_000)
    lead_time_days: int = Field(default=2, ge=0, le=90)
    images: list[str] = Field(min_length=1, max_length=8, description="Ids from /vendor/uploads/images")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        seen, outl = set(), []
        for t in v:
            t = t.strip().lower()[:40]
            if t and t not in seen:
                seen.add(t)
                outl.append(t)
        return outl

    @model_validator(mode="after")
    def _prices(self):
        if self.selling_price > self.mrp:
            raise ValueError("Selling price must be at or below MRP")
        return self


class ProductIn(ProductBase):
    stock: int = Field(ge=0, le=1_000_000)
    discount_pct: float = Field(default=0, ge=0, le=90)


class ProductPatch(BaseModel):
    """Any content field; price, photo or description edits on a live product go to review."""
    name: str | None = None
    brand: str | None = None
    category: Category | None = None
    hsn_code: str | None = None
    tags: list[str] | None = None
    short_description: str | None = None
    description: str | None = None
    ingredients: str | None = None
    dosage: str | None = None
    mrp: float | None = None
    selling_price: float | None = None
    supply_cost: float | None = None
    gst_pct: float | None = None
    moq: int | None = None
    lead_time_days: int | None = None
    images: list[str] | None = None


class StockPatch(BaseModel):
    """Applies at once, no review."""
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    in_stock: bool | None = None
    discount_pct: float | None = Field(default=None, ge=0, le=90)


class ReviewNote(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class ApproveIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class HsnRateIn(BaseModel):
    gst_pct: float = Field(ge=0, le=40)
    description: str | None = Field(default=None, max_length=200)


ProductStatus = Literal["awaiting_review", "changes_requested", "listed", "rejected", "removed"]
