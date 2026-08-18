"""Data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

Condition = Literal["new", "like_new", "used", "for_parts", "unknown"]


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    description: str
    price: Decimal
    currency: str
    country: str | None
    city: str | None
    lat: float | None
    lon: float | None
    seller_hash: str | None
    seller_age_days: int | None
    seller_ratings: int | None
    seller_score: float | None
    ships: bool | None
    pickup_only: bool
    is_business: bool | None
    photos: list[str]
    posted_at: datetime | None
    seen_at: datetime
    raw_path: str | None = None


@dataclass
class Extraction:
    key: str | None
    category: str | None
    certainty: float
    condition: Condition
    defects: list[str] = field(default_factory=list)
    box_only: bool = False
    for_parts: bool = False
    wanted_ad: bool = False
    bundle_items: list[str] = field(default_factory=list)
    method: Literal["rules", "llm"] = "rules"
    notes: list[str] = field(default_factory=list)  # e.g. "llm_unavailable"


@dataclass
class Deal:
    id: int | None
    listing_ref: int
    key: str | None
    tier: int
    star: bool
    effort: str
    confidence: str
    cons_net_pln: Decimal
    exp_net_pln: Decimal
    cons_net_usd: Decimal
    exp_net_usd: Decimal
    capital_usd: Decimal
    breakdown_json: str
    evidence_json: str
    reasons: list[str]
    created_at: datetime
    verified_at: datetime | None = None
    status: str = "new"
