"""Paired scoring on SYNTHETIC errors: a failure removes the sample for every model."""

import math

import polars as pl

from maritime_forecast.evaluation.paired import paired


def test_failure_removes_sample_for_all_models():
    df = pl.DataFrame(
        {
            "evaluable": [True, True, True, False],
            "err_a": [1.0, math.nan, 3.0, math.nan],
            "err_b": [1.0, 2.0, None, 4.0],
        }
    )
    out = paired(df, ["err_a", "err_b"])
    assert out["paired"].to_list() == [True, False, False, False]
    assert out["err_a_failed"].to_list() == [
        False,
        True,
        False,
        False,
    ]  # not evaluable -> not a failure
    assert out["err_b_failed"].to_list() == [False, False, True, False]
    # identical sample sets
    p = out.filter("paired")
    assert p["err_a"].len() == p["err_b"].len() == 1
