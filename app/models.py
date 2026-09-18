"""Pydantic schemas for the batch reconciliation API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_TRANSACTIONS_PER_SIDE = 12


@lru_cache(maxsize=256)
def get_timezone(name: str) -> ZoneInfo:
    """Return a (cached) IANA time zone.

    ``ZoneInfo`` instances are immutable and the library docs recommend
    caching them; the cache also lets request models share one instance.
    """
    return ZoneInfo(name)


class TransactionIn(BaseModel):
    """One fund-flow record.

    - ``id``: batch-unique identifier (non-empty string).
    - ``time``: ISO-8601 timestamp; naive values are treated as UTC.
    - ``amount``: non-zero signed integer amount in cents.
    """

    id: StrictStr = Field(min_length=1, max_length=128)
    time: datetime
    amount: StrictInt

    @field_validator("amount")
    @classmethod
    def amount_must_be_non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("amount must be a non-zero integer (cents)")
        return v

    def epoch_seconds(self) -> float:
        """Return the timestamp as UTC epoch seconds."""
        dt = self.time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


class LockedGroupIn(BaseModel):
    """An operator-confirmed group, referencing transactions of this batch.

    ``left_ids`` must reference ids from ``left`` and ``right_ids`` ids from
    ``right``; the referenced transactions must form a legal group. Semantic
    validation (unknown ids, reuse across groups, group legality) happens in
    the endpoint and returns 422 with the offending group's index.
    """

    left_ids: list[StrictStr] = Field(min_length=1, max_length=3)
    right_ids: list[StrictStr] = Field(min_length=1, max_length=3)


class ExcludedPairIn(BaseModel):
    """An operator-declared pair that must never share a reconciliation group.

    ``left_id`` must reference an id from ``left`` and ``right_id`` an id from
    ``right``.  Semantic validation (unknown ids, wrong side, duplicate pairs)
    happens in the endpoint and returns 422 with the offending pair's index.
    """

    left_id: StrictStr = Field(min_length=1, max_length=128)
    right_id: StrictStr = Field(min_length=1, max_length=128)


class ReconcileRequest(BaseModel):
    """Batch of fund flows from two sides (at most 12 each)."""

    left: list[TransactionIn] = Field(default_factory=list, max_length=MAX_TRANSACTIONS_PER_SIDE)
    right: list[TransactionIn] = Field(default_factory=list, max_length=MAX_TRANSACTIONS_PER_SIDE)
    locked_groups: list[LockedGroupIn] | None = Field(
        default=None,
        description="Optional operator-confirmed groups forced into the solution.",
    )
    excluded_pairs: list[ExcludedPairIn] | None = Field(
        default=None,
        description=(
            "Optional (left_id, right_id) pairs that must never end up in the "
            "same reconciliation group."
        ),
    )


class GroupOut(BaseModel):
    """One reconciliation group: sorted id lists of both sides."""

    left_ids: list[str]
    right_ids: list[str]
    amount: int  # signed reconciled amount in cents (sum of either side)


class ReconcileResponse(BaseModel):
    groups: list[GroupOut]
    total_amount: int  # sum of absolute reconciled amounts across groups
    unmatched_left_ids: list[str]
    unmatched_right_ids: list[str]


class UnmatchedSummaryRequest(ReconcileRequest):
    """A reconcile batch plus the IANA time zone used to bucket unmatched flows.

    ``timezone`` must be an IANA time-zone name (e.g. ``Asia/Shanghai`` or
    ``America/New_York``); unknown names are rejected with 422.
    """

    timezone: StrictStr = Field(
        min_length=1,
        max_length=64,
        description="IANA time zone used to attribute unmatched flows to local dates.",
    )

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, v: str) -> str:
        try:
            get_timezone(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be a valid IANA time zone name") from None
        return v


class SignBucket(BaseModel):
    """Count and absolute amount of unmatched flows of one sign."""

    count: int
    amount: int  # sum of absolute amounts (cents)


class DaySummary(BaseModel):
    """Unmatched flows attributed to one local date."""

    date: date
    positive: SignBucket
    negative: SignBucket


class UnmatchedSummaryResponse(BaseModel):
    timezone: str
    days: list[DaySummary]  # ascending local date
    total: SignBucket  # all unmatched flows, both signs
    positive: SignBucket
    negative: SignBucket
