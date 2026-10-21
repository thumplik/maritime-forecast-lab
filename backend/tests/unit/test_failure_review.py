"""Failure-review categorization on hand-built SYNTHETIC records (no data, no network)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from maritime_forecast.evaluation import failure_review as fr
from maritime_forecast.geo.geodesic import direct
from maritime_forecast.schemas.forecast import ModelForecast

T0 = datetime(2023, 6, 24, tzinfo=UTC)
# As a run records them (CellResult.best_baseline_id); the underway cells name CV.
BEST = {("underway:ALL", 1.0): "constant_velocity_v1",
        ("underway:TRANSITING", 1.0): "constant_velocity_v1",
        ("stationary:ALL", 1.0): "persistence_v1"}  # fmt: skip
A = (29.0, -94.5)


def _pt(bearing, dist):
    la, lo = direct(*A, bearing, dist)
    return float(la), float(lo)


def _forecast(cands, central="ref1", ends=None):
    """cands: list of per-step (lat, lon) lists, 2 steps."""
    tt = [T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    ends = ends or {}
    trajs = [
        {"trajectory_id": f"ref{i + 1}", "similarity": 0.9 - 0.1 * i,
         "reference_segment_ids": [f"s{i}"], "reference_ends_at": ends.get(i),
         "points": [{"timestamp": t, "latitude_deg": la, "longitude_deg": lo}
                    for t, (la, lo) in zip(tt, c, strict=True)]}
        for i, c in enumerate(cands)
    ]  # fmt: skip
    c = trajs[int(central[3:]) - 1]
    return ModelForecast(model_id=fr.MODEL, model_version="1", status="ok", target_times=tt,
                         points=c["points"], trajectories=trajs,
                         central_trajectory_id=central)  # fmt: skip


def test_step_diagnostics_counts_far_candidates_and_holds():
    near, east, west = _pt(0, 5000), _pt(90, 20_000), _pt(270, 20_000)
    f = _forecast([[near, near], [near, east], [near, west], [near, near]],
                  ends={0: T0 + timedelta(hours=1)})  # fmt: skip
    d1 = fr.step_diagnostics(f, 0, *A)
    assert not d1.held and d1.n_far == 0 and d1.n_covered == 4
    d2 = fr.step_diagnostics(f, 1, *A)
    assert d2.held  # the central reference ended at 1 h
    assert d2.n_covered == 3 and d2.n_far == 2  # east and west split from the central


def _records(rows):
    """rows: (cutoff, motion, model, err, along, cross, pred, truth)."""
    out = []
    for cid, motion, m, err, al, cr, pred, truth in rows:
        out.append({
            "cutoff_id": cid, "horizon_h": 1.0, "motion_set": motion, "vessel_class": "cargo",
            "transiting": motion == "underway", "model_id": m, "status": "ok", "paired": True,
            "error_m": err, "along_m": al, "cross_m": cr, "pred_lat": pred[0],
            "pred_lon": pred[1], "truth_lat": truth[0], "truth_lon": truth[1],
        })  # fmt: skip
    return pl.DataFrame(out)


def _diag(cid, pred, held=False, n_far=0, cands=(), ctx=3, age=1.0, fallback=False):
    return {"cutoff_id": cid, "horizon_h": 1.0, "rr_lat": pred[0], "rr_lon": pred[1],
            "held": held, "n_far": n_far, "n_covered": len(cands),
            "cand_lat": [c[0] for c in cands], "cand_lon": [c[1] for c in cands],
            "context_steps": ctx, "anchor_age_min": age, "radius_fallback": fallback,
            "max_similarity": 0.5}  # fmt: skip


def _case():
    moved, stay, far = _pt(0, 3000), _pt(0, 100), _pt(90, 8000)
    truth_u = _pt(0, 10_000)
    rec = []
    # 0: underway, retrieval on the wrong branch, a candidate had it; CV better.
    rec += [(0, "underway", fr.MODEL, 9000.0, 100.0, 9000.0, far, truth_u)]
    rec += [
        (0, "underway", m, e, 0.0, 0.0, A, truth_u)
        for m, e in zip(fr.BASELINES, (10_000.0, 2000.0, 5000.0), strict=True)
    ]
    # 1: stationary vessel that stayed; retrieval departed (false departure).
    rec += [(1, "stationary", fr.MODEL, 2900.0, 2900.0, 0.0, moved, stay)]
    rec += [
        (1, "stationary", m, e, 0.0, 0.0, A, stay)
        for m, e in zip(fr.BASELINES, (100.0, 150.0, 120.0), strict=True)
    ]
    # 2: stationary vessel that departed.
    rec += [(2, "stationary", fr.MODEL, 50.0, 0.0, 0.0, moved, moved)]
    rec += [
        (2, "stationary", m, e, 0.0, 0.0, A, moved)
        for m, e in zip(fr.BASELINES, (3000.0, 3000.0, 3000.0), strict=True)
    ]
    cutoffs = pl.DataFrame({"cutoff_id": [0, 1, 2], "latitude_deg": [A[0]] * 3,
                            "longitude_deg": [A[1]] * 3})  # fmt: skip
    diag = pl.DataFrame([
        _diag(0, far, n_far=3, cands=[far, truth_u], fallback=True),
        _diag(1, moved, held=True, ctx=1),
        _diag(2, moved),
    ])  # fmt: skip
    return _records(rec), cutoffs, diag


def test_flags_and_best_baseline_per_cell():
    records, cutoffs, diag = _case()
    s = fr.flag_samples(records, cutoffs, diag, BEST)
    u = s.filter((pl.col("stratum") == "underway:ALL") & (pl.col("cutoff_id") == 0)).row(
        0, named=True
    )
    assert u["base_model"] == "constant_velocity_v1" and u["base_err"] == 2000.0 and u["loss"]
    assert u["branch_disagreement"] and u["wrong_branch"] and u["new_geography"]
    assert not u["timing_dominated"] and not u["departure"]  # cross-track dominated
    st = s.filter(pl.col("stratum") == "stationary:ALL").sort("cutoff_id")
    one, two = st.row(0, named=True), st.row(1, named=True)
    assert one["false_departure"] and not one["departure"] and one["references_exhausted"]
    assert one["sparse_history"] and one["timing_dominated"]
    assert two["departure"] and not two["false_departure"] and not two["loss"]
    assert set(s["stratum"]) == {"underway:ALL", "underway:TRANSITING", "stationary:ALL"}


def test_summary_shares_and_losses():
    records, cutoffs, diag = _case()
    summ = fr.summarize(fr.flag_samples(records, cutoffs, diag, BEST))
    st = summ.filter(pl.col("stratum") == "stationary:ALL")
    allrow = st.filter(pl.col("category") == "all").row(0, named=True)
    assert allrow["n"] == 2 and allrow["loss_rate"] == 0.5
    fd = st.filter(pl.col("category") == "false_departure").row(0, named=True)
    assert fd["n"] == 1 and fd["share_of_losses"] == 1.0
    assert np.isclose(fd["median_diff_km"], (2900 - 100) / 1000)


def test_refuses_when_the_rerun_does_not_reproduce_the_run():
    records, cutoffs, diag = _case()
    bad = diag.with_columns(rr_lat=pl.col("rr_lat") + 1e-6)
    with pytest.raises(ValueError, match="reproduce"):
        fr.flag_samples(records, cutoffs, bad, BEST)
    with pytest.raises(ValueError, match="every paired"):
        fr.flag_samples(records, cutoffs, diag.filter(pl.col("cutoff_id") != 2), BEST)


def test_no_skill_reference_scores_no_losses_and_control_is_reported():
    records, cutoffs, diag = _case()
    best = {**BEST, ("stationary:ALL", 1.0): None}
    samples = fr.flag_samples(records, cutoffs, diag, best)
    st = samples.filter(pl.col("stratum") == "stationary:ALL")
    assert st["loss"].null_count() == st.height and (st["base_model"] == "persistence_v1").all()
    summ = fr.summarize(samples)
    row = summ.filter((pl.col("stratum") == "stationary:ALL") & (pl.col("category") == "all"))
    assert row["loss_rate"][0] is None and not row["has_reference"][0]
    assert "wrong_branch_control" in fr.CATEGORIES and "control_candidate_err_m" in samples.columns
    assert summ.filter(pl.col("category") == "false_departure")["selects_on_truth"].all()
