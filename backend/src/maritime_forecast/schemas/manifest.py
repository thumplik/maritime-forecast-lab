"""Split manifests: named, versioned protocol definitions (build plan §6.2).

A manifest is a *rule* (time periods with purge gaps and/or a hash-based vessel assignment)
plus the materialized vessel lists it produced on a dataset, for auditability. Membership is
always decided by the rule, so a manifest also answers for vessels not seen at build time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Protocol = Literal["P-known", "P-unseen", "P-paper", "P-region"]
SplitName = Literal["train", "calibration", "test"]
MANIFEST_VERSION = "splits-v1"


class Period(BaseModel):
    """Half-open UTC interval [start, end) assigned to one split."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    split: SplitName
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("period bounds must be timezone-aware UTC")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Period:
        if self.end <= self.start:
            raise ValueError("period end must be after start")
        return self

    def contains(self, t: datetime) -> bool:
        return self.start <= t < self.end


class VesselAssignment(BaseModel):
    """Deterministic vessel -> split by SHA-256 of (salt + vessel_id) mod 1000.

    `buckets` maps each split to a half-open range [lo, hi) of 0..1000; ranges must tile
    0..1000 exactly, which makes the vessel sets disjoint by construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    method: Literal["sha256_mod1000"] = "sha256_mod1000"
    salt: str = ""
    buckets: dict[SplitName, tuple[int, int]]

    @model_validator(mode="after")
    def _tiles(self) -> VesselAssignment:
        spans = sorted(self.buckets.values())
        edges = [0]
        for lo, hi in spans:
            if lo != edges[-1] or hi <= lo:
                raise ValueError(f"vessel buckets must tile 0..1000 without gaps: {spans}")
            edges.append(hi)
        if edges[-1] != 1000:
            raise ValueError(f"vessel buckets must end at 1000: {spans}")
        return self

    @staticmethod
    def bucket_of(vessel_id: str, salt: str = "") -> int:
        return int(hashlib.sha256((salt + vessel_id).encode()).hexdigest(), 16) % 1000

    def assign(self, vessel_id: str) -> SplitName:
        b = self.bucket_of(vessel_id, self.salt)
        for split, (lo, hi) in self.buckets.items():
            if lo <= b < hi:
                return split
        raise AssertionError("unreachable: buckets tile 0..1000")


class SplitManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Protocol
    version: str = MANIFEST_VERSION
    dataset_id: str
    description: str
    periods: list[Period] | None = None
    purge_hours: float = Field(default=0.0, ge=0)
    vessel_assignment: VesselAssignment | None = None
    preprocessing: dict = Field(default_factory=dict)
    vessels: dict[SplitName, list[str]] = Field(
        default_factory=dict, description="Materialized vessel lists observed in the dataset"
    )
    counts: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    git_sha: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SplitManifest:
        if self.periods is None and self.vessel_assignment is None:
            raise ValueError("a manifest needs time periods, a vessel assignment, or both")
        if self.periods:
            ps = sorted(self.periods, key=lambda p: p.start)
            purge = timedelta(hours=self.purge_hours)
            for a, b in zip(ps, ps[1:], strict=False):
                if b.start < a.end + purge:
                    raise ValueError(
                        f"periods {a.split} and {b.split} overlap or are closer than the "
                        f"{self.purge_hours} h purge gap"
                    )
        if self.protocol == "P-unseen" and not (self.periods and self.vessel_assignment):
            raise ValueError("P-unseen needs both a chronological split and vessel assignment")
        if self.protocol == "P-known" and self.vessel_assignment is not None:
            raise ValueError("P-known shares vessels across splits; no vessel assignment")
        listed = [v for vs in self.vessels.values() for v in vs]
        if self.vessel_assignment is not None and len(listed) != len(set(listed)):
            raise ValueError("materialized vessel lists overlap")
        return self

    # -- membership ---------------------------------------------------------------------
    def time_split(self, t: datetime) -> SplitName | None:
        if self.periods is None:
            return None
        return next((p.split for p in self.periods if p.contains(t)), None)

    def split_of(self, vessel_id: str, t: datetime | None = None) -> SplitName | None:
        """The split a (vessel, time) point belongs to, or None if it is purged/excluded."""
        v = self.vessel_assignment.assign(vessel_id) if self.vessel_assignment else None
        if self.periods is None:
            return v
        if t is None:
            raise ValueError(f"{self.protocol} has a time split; pass t")
        ts = self.time_split(t)
        if ts is None or (v is not None and v != ts):
            return None
        return ts

    def window_split(self, vessel_id: str, start: datetime, end: datetime) -> SplitName | None:
        """Split for a whole context+label window; None if its ends fall in different splits
        or in a purge gap (build plan §6.2: purge windows that overlap split boundaries)."""
        a = self.split_of(vessel_id, start if self.periods else None)
        b = self.split_of(vessel_id, end if self.periods else None)
        return a if a == b else None
