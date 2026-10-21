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
# up to the bracket gap (30 min) past the horizon; for non-test splits truth is capped at the
# split's end (fits, and benchmarks of dev/calibration), so it never reads a later split.
JUNE_2023_PERIODS = [
    Period(split="train", start=_day(1), end=_day(17)),
    Period(split="calibration", start=_day(18), end=_day(22)),
    Period(split="test", start=_day(23), end=_day(1, month=7)),
]
PURGE_HOURS = 24.0  # >= longest first-class horizon, so labels never reach the next period

# June + July 2023 (dataset noaa_us_gulf_2023-06-07), added after the Milestone 3 failure review
# read the June test results (docs/failure-review.md "Test-set use"). July is a fresh, untouched
# test period. The June days are re-purposed: the old calibration period becomes the DEV window
# (model development), the old June test becomes CALIBRATION (Milestone 4 conformal radii, which
# must be disjoint from the index period and the test; it is not a held-out result any more).
# Train is unchanged, so the retrieval index period and fitted train parameters cover the same
# days as on the June-only dataset. 24 h purges separate every period.
JUNE_JULY_2023_PERIODS = [
    Period(split="train", start=_day(1), end=_day(17)),
    Period(split="dev", start=_day(18), end=_day(22)),
    Period(split="calibration", start=_day(23), end=_day(1, month=7)),
    # Ends Jul 31 00:00: the data end on Jul 31, so a later end would leave the last targets
    # without a report after them (not evaluable for a reason that is really "data ended").
    Period(split="test", start=_day(2, month=7), end=_day(31, month=7)),
]
JUNE_JULY_DATASET = "noaa_us_gulf_2023-06-07"
HOLDOUT_MANIFEST_VERSION = "splits-v2"

P_UNSEEN_ASSIGNMENT = VesselAssignment(
    salt="p-unseen-v1", buckets={"train": (0, 700), "calibration": (700, 800), "test": (800, 1000)}
)
# Same salt and the same train and test vessels; the old calibration bucket is halved into dev
# and calibration.
P_UNSEEN_ASSIGNMENT_V2 = VesselAssignment(
    salt="p-unseen-v1",
    buckets={"train": (0, 700), "dev": (700, 750), "calibration": (750, 800), "test": (800, 1000)},
)


def _periods(dataset_id: str) -> tuple[list[Period], str]:
    """The chronological periods (and manifest version) for a dataset."""
    if dataset_id == JUNE_JULY_DATASET:
        return JUNE_JULY_2023_PERIODS, HOLDOUT_MANIFEST_VERSION
    return JUNE_2023_PERIODS, "splits-v1"


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
    periods, version = _periods(dataset_id)
    return SplitManifest(
        protocol="P-known",
        version=version,
        dataset_id=dataset_id,
        description="Chronological split; a vessel's own earlier voyages are allowed.",
        periods=periods,
        purge_hours=PURGE_HOURS,
    )


def p_unseen(dataset_id: str) -> SplitManifest:
    periods, version = _periods(dataset_id)
    v2 = dataset_id == JUNE_JULY_DATASET
    return SplitManifest(
        protocol="P-unseen",
        version=version,
        dataset_id=dataset_id,
        description="Chronological split AND vessel-disjoint by salted MMSI hash "
        + ("(70/5/5/20 train/dev/calibration/test)" if v2 else "(70/10/20 train/calibration/test)")
        + ". A sample counts only where both agree.",
        periods=periods,
        purge_hours=PURGE_HOURS,
        vessel_assignment=P_UNSEEN_ASSIGNMENT_V2 if v2 else P_UNSEEN_ASSIGNMENT,
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
        version=_periods(dataset_id)[1],
        periods=_periods(dataset_id)[0],
        purge_hours=PURGE_HOURS,
        region_split=RegionSplit(
            test_region=Box(**region(P_REGION_TEST).as_dict()), buffer_km=P_REGION_BUFFER_KM
        ),
    )


BUILDERS = {"P-known": p_known, "P-unseen": p_unseen, "P-region": p_region, "P-paper": p_paper}
