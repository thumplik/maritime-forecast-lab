"""Cutoff sampler on SYNTHETIC candidate rows."""

from datetime import UTC, datetime, timedelta

import polars as pl

from maritime_forecast.data.cutoffs import SamplerConfig, sample_cutoffs


def _cands(n_vessels=3, hours=48, sog=8.0):
    t0 = datetime(2023, 6, 1, tzinfo=UTC)
    rows = [
        {
            "vessel_id": f"36700000{v}",
            "observed_at": t0 + timedelta(minutes=10 * k),
            "sog_mean_10m": sog,
            "n_reports_3h": 18,
        }
        for v in range(n_vessels)
        for k in range(hours * 6)
    ]
    return pl.DataFrame(rows)


def test_spacing_and_daily_cap():
    cfg = SamplerConfig()
    out = sample_cutoffs(_cands(), cfg)
    for _, g in out.group_by("vessel_id"):
        t = g["observed_at"].sort().to_list()
        assert all((b - a) >= timedelta(hours=3) for a, b in zip(t, t[1:], strict=False))
        per_day = g.group_by(pl.col("observed_at").dt.date()).len()["len"]
        assert per_day.max() <= cfg.max_per_vessel_day
    assert out.height == 3 * 2 * 2  # 3 vessels x 2 days x cap 2


def test_deterministic_with_seed():
    a = sample_cutoffs(_cands(), SamplerConfig(seed=1))
    b = sample_cutoffs(_cands(), SamplerConfig(seed=1))
    assert a.equals(b)


def test_motion_sets_disjoint():
    c = pl.concat([_cands(1, sog=8.0), _cands(1, sog=0.1).with_columns(vessel_id=pl.lit("x"))])
    u = sample_cutoffs(c, SamplerConfig(motion_set="underway"))
    s = sample_cutoffs(c, SamplerConfig(motion_set="stationary"))
    assert set(u["vessel_id"]) == {"367000000"} and set(s["vessel_id"]) == {"x"}


def test_min_history():
    c = _cands(1).with_columns(n_reports_3h=pl.lit(2))
    assert sample_cutoffs(c, SamplerConfig()).height == 0
