"""Split manifests: named, versioned protocol definitions (build plan §6.2).

A manifest is a *rule* (time periods with purge gaps and/or a hash-based vessel assignment)
plus the materialized vessel lists it produced on a dataset, for auditability. Membership is
always decided by the rule, so a manifest also answers for vessels not seen at build time.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
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


# Shortest meridian degree (at the equator), so the latitude pad is never under buffer_km.
KM_PER_DEG_LAT = 110.574
Side = Literal["train", "test"]


class Box(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @model_validator(mode="after")
    def _valid(self) -> Box:
        ok_lat = -90 <= self.lat_min < self.lat_max <= 90
        ok_lon = -180 <= self.lon_min < self.lon_max <= 180
        if not (ok_lat and ok_lon):
            raise ValueError(f"invalid box {self}")
        return self


class RegionSplit(BaseModel):
    """Geographic cut for P-region: inside `test_region` is test; beyond a `buffer_km` band
    around it is train; the band itself is excluded so near-boundary traffic can't leak."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    test_region: Box
    buffer_km: float = Field(ge=0)

    @property
    def outer(self) -> tuple[float, float, float, float]:
        """(lat_min, lat_max, lon_min, lon_max) of the test region grown by the buffer.
        Both pads are conservative: the shortest degree of latitude, and the longitude degree
        at the grown box's poleward edge."""
        r = self.test_region
        dlat = self.buffer_km / KM_PER_DEG_LAT
        coslat = math.cos(math.radians(min(89.0, max(abs(r.lat_min), abs(r.lat_max)) + dlat)))
        dlon = self.buffer_km / (KM_PER_DEG_LAT * coslat)
        return r.lat_min - dlat, r.lat_max + dlat, r.lon_min - dlon, r.lon_max + dlon

    def side(self, lat: float, lon: float) -> Side | None:
        r = self.test_region
        if r.lat_min <= lat <= r.lat_max and r.lon_min <= lon <= r.lon_max:
            return "test"
        a, b, c, d = self.outer
        if a <= lat <= b and c <= lon <= d:
            return None  # buffer band
        return "train"

    def sql_case(self, lat: str = "latitude_deg", lon: str = "longitude_deg") -> str:
        """SQL twin of side(): 'test', 'train' or NULL (buffer, or missing position)."""
        r = self.test_region
        a, b, c, d = self.outer
        return (
            f"CASE WHEN {lat} IS NULL OR {lon} IS NULL THEN NULL "
            f"WHEN {lat} BETWEEN {r.lat_min!r} AND {r.lat_max!r} "
            f"AND {lon} BETWEEN {r.lon_min!r} AND {r.lon_max!r} THEN 'test' "
            f"WHEN {lat} BETWEEN {a!r} AND {b!r} AND {lon} BETWEEN {c!r} AND {d!r} THEN NULL "
            "ELSE 'train' END"
        )


class SplitManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Protocol
    version: str = MANIFEST_VERSION
    dataset_id: str
    description: str
    periods: list[Period] | None = None
    purge_hours: float = Field(default=0.0, ge=0)
    vessel_assignment: VesselAssignment | None = None
    region_split: RegionSplit | None = None
    preprocessing: dict = Field(default_factory=dict)
    vessels: dict[SplitName, list[str]] = Field(
        default_factory=dict,
        description="AUDIT ONLY. Vessels observed in each split; carries no time bound, so "
        "never filter data by these lists. Membership is always split_of()/window_split().",
    )
    counts: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    git_sha: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SplitManifest:
        if self.periods is None and self.vessel_assignment is None and self.region_split is None:
            raise ValueError("a manifest needs time periods, a vessel assignment or a region split")
        if (self.protocol == "P-region") != (self.region_split is not None):
            raise ValueError("region_split is required for P-region and only allowed there")
        if self.region_split is not None and self.vessel_assignment is not None:
            raise ValueError("P-region splits by place and time only; no vessel assignment")
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

    def split_of(
        self,
        vessel_id: str,
        t: datetime | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> SplitName | None:
        """The split a (vessel, time[, position]) point belongs to, or None if it is excluded
        (purge gap, disagreeing vessel bucket, or the P-region buffer band).

        P-region: the test side pairs only with the test period; the train side pairs with
        the train and calibration periods.
        """
        v = self.vessel_assignment.assign(vessel_id) if self.vessel_assignment else None
        if self.periods is None:
            return v
        if t is None:
            raise ValueError(f"{self.protocol} has a time split; pass t")
        ts = self.time_split(t)
        if ts is None or (v is not None and v != ts):
            return None
        if self.region_split is not None:
            if lat is None or lon is None:
                raise ValueError("P-region needs the point's lat/lon")
            return self._pair(ts, self.region_split.side(lat, lon))
        return ts

    @staticmethod
    def _pair(time_split: SplitName | None, side: Side | None) -> SplitName | None:
        if time_split is None or side is None or (side == "test") != (time_split == "test"):
            return None
        return time_split

    def split_for_side(self, t: datetime, side: Side | None) -> SplitName | None:
        """P-region membership when the side was already classified (e.g. by sql_case())."""
        if self.region_split is None:
            raise ValueError("split_for_side is only for P-region")
        return self._pair(self.time_split(t), side)

    def window_split(
        self,
        vessel_id: str,
        start: datetime,
        end: datetime,
        context_positions: Sequence[tuple[float, float]] | None = None,
        label_positions: Sequence[tuple[float, float]] = (),
    ) -> SplitName | None:
        """Split for a context+label window, or None if excluded: its ends fall in different
        splits or in a purge gap (build plan §6.2).

        P-region: membership is decided by the CONTEXT positions (reports at or before the
        cutoff), which must all lie on one side and outside the buffer. Label positions never
        decide test membership, because that would select test samples on their future
        (whether the vessel stays in the box); truth is scored anywhere in the truth region.
        For train and calibration windows the label positions must also stay on the train
        side, so no training label comes from the test waters or the buffer.
        """
        if self.region_split is not None:
            if not context_positions:
                raise ValueError("P-region needs the window's context positions")
            sides = {self.region_split.side(la, lo) for la, lo in context_positions}
            if len(sides) != 1 or None in sides:
                return None
            (side,) = sides
            if side == "train" and any(
                self.region_split.side(la, lo) != "train" for la, lo in label_positions
            ):
                return None
            a, b = self.split_for_side(start, side), self.split_for_side(end, side)
            return a if a == b else None
        a = self.split_of(vessel_id, start if self.periods else None)
        b = self.split_of(vessel_id, end if self.periods else None)
        return a if a == b else None
