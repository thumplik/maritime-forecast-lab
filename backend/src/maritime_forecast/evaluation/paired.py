"""Paired scoring (build plan §6.5, AGENTS.md rule 8).

Every model in a comparison is scored on the identical set of evaluable samples. A model that
fails on a sample (NaN/null error) is recorded as a failure, and the sample is removed for
*all* models, never silently for one.
"""

from __future__ import annotations

import polars as pl


def paired(
    recs: pl.DataFrame, error_cols: list[str], evaluable_col: str = "evaluable"
) -> pl.DataFrame:
    """Add `<col>_failed` per model and a `paired` mask: evaluable and no model failed.

    NaN is treated as null, so failures cannot hide as NaN through nan-aware statistics.
    """
    failed = {
        f"{c}_failed": pl.col(evaluable_col) & (pl.col(c).is_null() | pl.col(c).is_nan())
        for c in error_cols
    }
    out = recs.with_columns(**failed)
    any_failed = pl.any_horizontal(*(pl.col(name) for name in failed))
    return out.with_columns(paired=pl.col(evaluable_col) & ~any_failed)
