"""Pydantic models for ingested data.

RawTrade tolerates extra fields so schema additions on Polymarket's side
don't crash the ingestor. The `tap` CLI command lets us inspect the live
wire format and tighten this model as needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Side = Literal["BUY", "SELL"]


class RawTrade(BaseModel):
    """Trade payload as it arrives from RTDS activity/trades."""

    model_config = ConfigDict(extra="allow")

    asset: str
    conditionId: str
    outcome: str
    outcomeIndex: int
    price: float
    side: Side
    size: float
    timestamp: int | str
    transactionHash: str
    eventSlug: str | None = None
    slug: str | None = None
    title: str | None = None

    @field_validator("transactionHash")
    @classmethod
    def _lower_hash(cls, v: str) -> str:
        return v.lower()


class NormalizedTrade(BaseModel):
    """Trade after normalization, ready for DB insert."""

    tx_hash: str
    log_index: int = 0
    occurred_at: datetime
    wallet: str | None
    condition_id: str
    asset_id: str
    outcome: str
    outcome_index: int
    side: Side
    price: float
    size_shares: float
    size_usdc: float = Field(..., description="size_shares * price")
