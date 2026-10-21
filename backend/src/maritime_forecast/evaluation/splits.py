"""Builders for the named protocol manifests (build plan §6.2; docs/evaluation-protocol.md)."""

from __future__ import annotations

from datetime import UTC, datetime

from maritime_forecast.config import region
from maritime_forecast.schemas.manifest import (
    Box,
    Period,
    RegionSplit,
    SplitManifest,
    VesselAssignment,
)


def _day(d: int, month: int = 6) -> datetime:
    return datetime(2023, month, d, tzinfo=UTC)


# Chronological split of the June 2023 calibration month. Gaps between periods are the purge:
# no cutoff window may touch them (window_split). Truth for a cutoff may bracket with a report
# up to the bracket gap (30 min) past the horizon; for non-test splits truth_grid is capped at
# the split's end, so a fit never reads a later split even with adjacent periods.
JUNE_2023_PERIODS = [
    Period(split="train", start=_day(1), end=_day(17)),
    Period(split="calibration", start=_day(18), end=_day(22)),
    Period(split="test", start=_day(23), end=_day(1, month=7)),
]
PURGE_HOURS = 24.0  # >= longest first-class horizon, so labels never reach the next period

P_UNSEEN_ASSIGNMENT = VesselAssignment(
    salt="p-unseen-v1", buckets={"train": (0, 700), "calibration": (700, 800), "test": (800, 1000)}
)
# Must reproduce scripts/calibrate.py's original rule (unsalted hash, first 20% = test), which
# the Milestone 0 calibration result was computed with.
P_PAPER_ASSIGNMENT = VesselAssignment(salt="", buckets={"test": (0, 200), "train": (200, 1000)})
P_PAPER_PREPROCESSING = {
    "region": "houston_galveston",
    "transceiver_class": ["A", "B"],
    "drop_sog_below_kn": 0.5,
    "gap_minutes": 30,
    "spike_max_implied_speed_kn": 50,
    "grid_step_minutes": 10,
    "n_context": 18,
    "n_target": 18,
    "window_stride": 18,
    "note": "Reconstruction from the paper's text (arXiv 2609.25827); manifests not obtained. "
    "See docs/coverage-audit.md §5.",
}


def p_known(dataset_id: str) -> SplitManifest:
    return SplitManifest(
        protocol="P-known",
        dataset_id=dataset_id,
        description="Chronological split; a vessel's own earlier voyages are allowed.",
        periods=JUNE_2023_PERIODS,
        purge_hours=PURGE_HOURS,
    )


def p_unseen(dataset_id: str) -> SplitManifest:
    return SplitManifest(
        protocol="P-unseen",
        dataset_id=dataset_id,
        description="Chronological split AND vessel-disjoint by salted MMSI hash "
        "(70/10/20 train/calibration/test). A sample counts only where both agree.",
        periods=JUNE_2023_PERIODS,
        purge_hours=PURGE_HOURS,
        vessel_assignment=P_UNSEEN_ASSIGNMENT,
    )


def p_paper(dataset_id: str) -> SplitManifest:
    return SplitManifest(
        protocol="P-paper",
        dataset_id=dataset_id,
        description="Calibration only: vessel-disjoint reconstruction of the calibration "
        "paper's Houston split. Not for reporting product performance.",
        vessel_assignment=P_PAPER_ASSIGNMENT,
        preprocessing=P_PAPER_PREPROCESSING,
    )


P_REGION_TEST = "houston_galveston"  # config/regions.yaml
P_REGION_BUFFER_KM = 50.0


def p_region(dataset_id: str) -> SplitManifest:
    return SplitManifest(
        protocol="P-region",
        dataset_id=dataset_id,
        description="Region-disjoint transfer: test inside Houston-Galveston, train beyond a "
        f"{P_REGION_BUFFER_KM:g} km buffer elsewhere in the Gulf; combined with the chronological "
        "periods. Membership is decided by context positions; train/calibration labels must also "
        "stay on the train side. Expect models with "
        "absolute-position inputs (TrAISformer) to fail here (build plan §5 N1).",
        periods=JUNE_2023_PERIODS,
        purge_hours=PURGE_HOURS,
        region_split=RegionSplit(
            test_region=Box(**region(P_REGION_TEST).as_dict()), buffer_km=P_REGION_BUFFER_KM
        ),
    )


BUILDERS = {"P-known": p_known, "P-unseen": p_unseen, "P-region": p_region, "P-paper": p_paper}
