"""Builders for the named protocol manifests (build plan §6.2; docs/evaluation-protocol.md)."""

from __future__ import annotations

from datetime import UTC, datetime

from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment


def _day(d: int, month: int = 6) -> datetime:
    return datetime(2023, month, d, tzinfo=UTC)


# Chronological split of the June 2023 calibration month. Gaps between periods are the purge:
# nothing in them is used, and windows touching them are excluded via window_split().
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


BUILDERS = {"P-known": p_known, "P-unseen": p_unseen, "P-paper": p_paper}
