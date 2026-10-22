"""Split-conformal radii (evaluation.conformal) on SYNTHETIC errors."""

import math

import numpy as np
import polars as pl

from maritime_forecast.evaluation.conformal import (
    ConformalConfig,
    assign_radii,
    conformal_quantile,
    coverage_table,
    fit_radii,
)


def test_conformal_quantile_is_the_ceil_rank_and_refuses_small_n():
    e = np.arange(1.0, 101.0)  # 1..100
    assert conformal_quantile(e, 0.9) == 91.0  # ceil(101 * 0.9) = 91
    assert conformal_quantile(e, 0.8) == 81.0
    assert conformal_quantile(np.arange(1.0, 9.0), 0.9) is None  # ceil(9 * 0.9) = 9 > 8


def _records(n_per_vessel, vessels, rng, scale, motion="underway", cls="tanker"):
    rows = []
    for v in range(vessels):
        for _ in range(n_per_vessel):
            rows.append({"cutoff_id": len(rows), "vessel_id": f"{cls}{v}", "motion_set": motion,
                         "vessel_class": cls, "horizon_h": 1.0, "model_id": "m",
                         "evaluable": True, "error_m": float(rng.exponential(scale))})  # fmt: skip
    return pl.DataFrame(rows)


def test_exchangeable_coverage_is_near_nominal_and_fallback_is_used():
    rng = np.random.default_rng(0)
    cal = pl.concat([_records(5, 200, rng, 1000.0),
                     _records(2, 5, rng, 5000.0, cls="fishing")])  # 10 < min_n  # fmt: skip
    test = pl.concat([_records(5, 200, rng, 1000.0),
                      _records(2, 50, rng, 5000.0, cls="fishing")])  # fmt: skip
    cfg = ConformalConfig(min_n=30)
    radii = fit_radii(cal, cfg)
    fish = radii.filter((pl.col("stratum") == "underway:fishing") & (pl.col("level") == 0.9))
    assert fish["radius_m"][0] is None and fish["n"][0] == 10
    a = assign_radii(test, radii, 0.9)
    assert set(a.filter(pl.col("vessel_class") == "fishing")["radius_stratum"]) == {"underway:ALL"}
    cov = coverage_table(a.filter(pl.col("vessel_class") == "tanker"), 0.9,
                         ["vessel_class"], cfg)  # fmt: skip
    row = cov.row(0, named=True)
    assert abs(row["coverage"] - 0.9) < 0.03 and row["ci_low"] < 0.9 < row["ci_high"]
    assert row["fallback_share"] == 0.0 and row["calibrated"] is True
    assert math.isclose(row["mean_area_km2"], math.pi * row["median_radius_km"] ** 2)
    # The fishing vessels got a pooled radius dominated by tankers: they under-cover, and the
    # table shows it (with the fallback share), rather than hiding it.
    f = coverage_table(a.filter(pl.col("vessel_class") == "fishing"), 0.9, ["vessel_class"], cfg)
    assert f["fallback_share"][0] == 1.0 and f["coverage"][0] < 0.8


def test_non_evaluable_records_are_ignored():
    rng = np.random.default_rng(1)
    cal = _records(5, 20, rng, 1000.0)
    bad = cal.with_columns(evaluable=pl.lit(False), error_m=pl.lit(None, dtype=pl.Float64))
    radii = fit_radii(pl.concat([cal, bad]), ConformalConfig(min_n=30))
    assert radii.filter(pl.col("stratum") == "ALL")["n"].unique().to_list() == [100]


def test_boundary_rank_full_fallback_and_unseen_stratum():
    from maritime_forecast.evaluation.conformal import _vessel_ci

    e = np.arange(1.0, 31.0)
    assert conformal_quantile(e, 0.95) == 30.0  # ceil(31 * 0.95) = 30 = n: the maximum
    rng = np.random.default_rng(2)
    cal = pl.concat([_records(3, 10, rng, 1000.0), _records(2, 5, rng, 900.0, motion="stationary")])
    radii = fit_radii(cal, ConformalConfig(min_n=30))
    # underway:tanker n=30 qualifies; stationary has only 10 (no class or motion radius) -> ALL.
    test = pl.concat(
        [
            _records(1, 3, rng, 1000.0, motion="stationary"),
            _records(1, 3, rng, 1000.0, cls="unknown"),
        ]
    )  # class absent in calibration
    a = assign_radii(test, radii, 0.9)
    got = dict(zip(a["vessel_id"] + "/" + a["motion_set"], a["radius_stratum"], strict=True))
    assert got["tanker0/stationary"] == "ALL"
    assert got["unknown0/underway"] == "underway:ALL"
    cov = coverage_table(a, 0.9, ["motion_set"], ConformalConfig(min_n=30))
    assert set(cov.columns) >= {"n_no_radius", "ci_contains_level", "n_vessels"}
    lo, hi = _vessel_ci(np.array([True, False, True]), np.array(["v", "v", "v"]), 200, 0)
    assert lo == hi == 2 / 3  # one vessel: the bootstrap cannot vary
    assert (
        "n_vessels" in radii.columns and radii["n_vessels"].max() == 10
    )  # ids repeat across motion


def test_records_without_a_radius_are_counted_not_hidden():
    rng = np.random.default_rng(3)
    cal = _records(1, 5, rng, 1000.0)  # 5 errors: below min_n everywhere -> no radius at all
    radii = fit_radii(cal, ConformalConfig(min_n=30))
    a = assign_radii(_records(2, 10, rng, 1000.0), radii, 0.9)
    assert a["radius_m"].null_count() == a.height


def test_region_for_refuses_hindsight_mismatches_and_unfitted_times():
    from datetime import UTC, datetime, timedelta

    from maritime_forecast.evaluation.conformal import motion_state, region_for
    from maritime_forecast.schemas.manifest import Period, SplitManifest

    t0 = datetime(2023, 7, 10, tzinfo=UTC)
    rep = pl.DataFrame({"observed_at": [t0 - timedelta(minutes=k) for k in (12, 8, 0)],
                        "sog_knots": [0.0, 3.0, 3.0]})  # fmt: skip
    assert motion_state(rep) == "underway"  # the 12-min-old report is outside the window
    assert motion_state(rep.with_columns(sog_knots=pl.lit(1.0))) is None
    assert motion_state(rep.with_columns(sog_knots=pl.lit(0.1))) == "stationary"
    man = SplitManifest(protocol="P-known", dataset_id="d", description="SYNTHETIC", periods=[
        Period(split="calibration", start=t0 - timedelta(days=9), end=t0 - timedelta(days=2))])  # fmt: skip
    params = {"calibration_set_id": "cal", "manifest_sha256": "abc", "config": {"levels": [0.9]},
              "models": {"m": {"model_version": "1"}},
              "radii": [{"model_id": "m", "horizon_h": h, "level": 0.9, "stratum": s, "radius_m": r}
                        for h, s, r in [(1.0, "ALL", 900.0), (2.0, "underway:ALL", 1500.0),
                                        (2.0, "underway:tanker", None)]]}  # fmt: skip
    tt = [t0 + timedelta(minutes=30 * k) for k in range(1, 5)]  # 0.5, 1, 1.5, 2 h
    reg, why = region_for(params, man, "abc", "m", "1", "underway", "tanker", t0, tt, 0.9)
    assert region_for(params, man, "abc", "m", "1", "underway", "tanker", t0, tt, 0.9,
                      config_sha256="other")[0] is None  # fitted for another config  # fmt: skip
    assert why is None and reg["radii_m"] == [None, 900.0, None, 1500.0]
    assert reg["strata"] == [None, "ALL", None, "underway:ALL"]  # tanker radius null -> fallback
    assert region_for(params, man, "zzz", "m", "1", "underway", "x", t0, tt, 0.9)[0] is None
    assert region_for(params, man, "abc", "m", "2", "underway", "x", t0, tt, 0.9)[0] is None
    assert region_for(params, man, "abc", "m", "1", None, "x", t0, tt, 0.9)[0] is None
    early = t0 - timedelta(days=3)  # before the calibration window ended: hindsight
    _, why = region_for(params, man, "abc", "m", "1", "underway", "x", early,
                        [early + timedelta(hours=1)], 0.9)  # fmt: skip
    assert "labels' reach" in why


def test_h3_bins_get_their_own_radius_and_thin_cells_fall_back():
    from maritime_forecast.evaluation.conformal import add_h3

    rng = np.random.default_rng(4)
    # Cell A (Bolivar Roads): 60 tight errors; cell B (far): 10 wide errors (thin).
    a = _records(6, 10, rng, 300.0)
    b = _records(2, 5, rng, 5000.0, cls="cargo")
    recs = pl.concat([a, b]).with_columns(cutoff_id=pl.int_range(0, a.height + b.height))
    cut = pl.DataFrame({"cutoff_id": list(range(recs.height)),
                        "latitude_deg": [29.34] * a.height + [26.0] * b.height,
                        "longitude_deg": [-94.74] * a.height + [-90.0] * b.height})  # fmt: skip
    recs = add_h3(recs.with_columns(pl.col("cutoff_id").cast(pl.Int64)),
                  cut.with_columns(pl.col("cutoff_id").cast(pl.Int64)))  # fmt: skip
    cfg = ConformalConfig(min_n=30)
    radii = fit_radii(recs, cfg, h3=True)
    cells = radii.filter(pl.col("stratum").str.contains(":h3:") & (pl.col("level") == 0.9))
    own = cells.filter(pl.col("radius_m").is_not_null())
    assert own.height == 1 and own["n"][0] == 60  # only cell A qualifies
    out = assign_radii(recs, radii, 0.9, h3=True)
    by = dict(zip(out["vessel_class"], out["radius_stratum"], strict=False))
    assert ":h3:" in by["tanker"] and by["cargo"] == "underway:ALL"  # thin cell -> slice-1 chain
    assert out.filter(pl.col("vessel_class") == "cargo")["s_first"][0].count(":h3:") == 1
    flat = assign_radii(recs, fit_radii(recs, cfg), 0.9)
    assert "h3_cell" in flat.columns and not flat["radius_stratum"].str.contains(":h3:").any()
