"""Per-stratum admission and transiting flag on SYNTHETIC records."""

import polars as pl

from maritime_forecast.data.cutoffs import transiting_expr
from maritime_forecast.evaluation.admission import AdmissionConfig, admit_by_stratum


def _recs(stratum, n, frac_eval, sog=5.0, ev_sog=None, vessels=50):
    rows = []
    for i in range(n):
        ev = i < frac_eval * n
        rows.append(
            {
                "cutoff_id": f"{stratum}{i}",
                "horizon_h": 6,
                "vessel_id": f"v{i % vessels}",
                "evaluable": ev,
                "stratum": stratum,
                "sog_mean_10m": (ev_sog if (ev and ev_sog is not None) else sog),
            }
        )
    return rows


def test_strata_judged_separately():
    recs = pl.DataFrame(_recs("tug", 1000, 0.95) + _recs("sail", 300, 0.40))
    out = {r["stratum"]: r for r in admit_by_stratum(recs).to_dicts()}
    assert out["tug"]["status"] == "first_class"
    assert out["sail"]["status"] == "experimental" and not out["sail"]["coverage_ok"]


def test_speed_survivorship_fails():
    # evaluable ones are all slow: survivorship bias
    recs = pl.DataFrame(_recs("x", 400, 0.6, sog=8.0, ev_sog=2.0))
    r = admit_by_stratum(recs).to_dicts()[0]
    assert r["coverage_ok"] and not r["speed_ok"] and r["status"] == "experimental"


def test_size_minimums():
    recs = pl.DataFrame(_recs("small", 50, 1.0, vessels=5))
    r = admit_by_stratum(recs, cfg=AdmissionConfig(min_evaluable=100)).to_dicts()[0]
    assert not r["size_ok"] and r["status"] == "experimental"


def test_transiting_expr():
    df = pl.DataFrame(
        {
            "vessel_class": ["cargo", "cargo", "tug_tow", "tanker", "passenger"],
            "sog_mean_30m": [10.0, 10.0, 10.0, 1.0, None],
            "sog_min_30m": [8.0, 0.1, 8.0, 0.8, 5.0],
        }
    )
    assert df.select(transiting_expr())["vessel_class"].to_list() == [
        True,
        False,
        False,
        False,
        False,
    ]
