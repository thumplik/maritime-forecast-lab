"""Split-conformal displacement radii (build plan §5 U1, Milestone 4).

For each model, horizon, nominal level and stratum (motion state x vessel class), the radius is
the conformal quantile of the model's displacement errors on a CALIBRATION benchmark run:

    k = ceil((n + 1) * level);  radius = k-th smallest error  (undefined if k > n)

so that, for an exchangeable new sample of the same stratum, P(error <= radius) >= level.
Calibration errors come from a benchmark of the protocol's calibration split, which is disjoint
from the retrieval index period (train) and from the test period (§6.2, §5 U1).

Fallback (decided before any test coverage was looked at): a (motion, class) stratum with fewer
than `min_n` calibration errors uses its motion state pooled over classes ("motion:ALL"), then
everything pooled ("ALL"). The stratum actually used is reported with every coverage figure.

Caveats, stated with every result:
  * The guarantee is marginal over samples and assumes exchangeability. Cutoffs of one vessel
    are correlated, and the calibration and test periods differ (e.g. retrieval's frozen pool is
    older at test time), so empirical coverage on the test period is the evidence, not the
    guarantee. Coverage CIs resample vessels.
  * Radii are per fitted horizon only (the benchmark's horizons).
  * Strata use the vessel class, which NOAA back-fills (not as-of data, §4); the benchmark
    stratifies the same way.
  * The fallback threshold counts errors, not vessels: one busy vessel can supply most of a
    stratum. `n_vessels` is reported with every radius so this is visible.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import numpy as np
import polars as pl

LEVELS = (0.8, 0.9, 0.95)
PRIMARY_LEVEL = 0.9
MIN_N = 30
TOLERANCE = 0.03  # |coverage - level| for a cell to count as calibrated ...
TOLERANCE_MIN_N = 200  # ... judged only on cells with at least this many test samples
ALL = "ALL"


@dataclass(frozen=True)
class ConformalConfig:
    levels: tuple[float, ...] = LEVELS
    min_n: int = MIN_N
    tolerance: float = TOLERANCE
    tolerance_min_n: int = TOLERANCE_MIN_N

    def as_dict(self) -> dict:
        d = asdict(self)
        d["levels"] = list(self.levels)
        return d


DEFAULT_CONFORMAL = ConformalConfig()


def conformal_quantile(errors: np.ndarray, level: float) -> float | None:
    """The split-conformal radius: the ceil((n+1) level)-th smallest error, or None if n is too
    small for that level (k > n)."""
    e = np.sort(np.asarray(errors, float))
    n = len(e)
    k = math.ceil((n + 1) * level)
    return float(e[k - 1]) if 1 <= k <= n else None


def _evaluable(records: pl.DataFrame) -> pl.DataFrame:
    """Per-model evaluable errors (truth exists and the model forecast)."""
    return records.filter(pl.col("evaluable") & pl.col("error_m").is_not_null())


H3_RES = 5  # pre-registered (implementation log, 2026-10-21): ~250 km^2 cells


def add_h3(records: pl.DataFrame, cutoffs: pl.DataFrame, res: int = H3_RES) -> pl.DataFrame:
    """Attach `h3_cell`: the H3 cell (resolution `res`) of each record's cutoff report position,
    known at the cutoff (causal). `cutoffs` has cutoff_id, latitude_deg, longitude_deg."""
    import h3

    cells = cutoffs.select(
        "cutoff_id",
        h3_cell=pl.struct("latitude_deg", "longitude_deg").map_elements(
            lambda r: h3.latlng_to_cell(r["latitude_deg"], r["longitude_deg"], res),
            return_dtype=pl.Utf8,
        ),
    )
    return records.join(cells, on="cutoff_id", how="left")


def _strata(records: pl.DataFrame, h3: bool) -> tuple[pl.DataFrame, list[str]]:
    """Evaluable records with their stratum columns, most specific first. Slice 1: motion:class,
    motion:ALL, ALL. H3 (slice 2): motion x H3 cell before those."""
    r = _evaluable(records).with_columns(
        s_class=pl.col("motion_set") + ":" + pl.col("vessel_class").fill_null("unknown"),
        s_motion=pl.col("motion_set") + ":" + pl.lit(ALL),
        s_all=pl.lit(ALL),
    )
    chain = ["s_class", "s_motion", "s_all"]
    if h3:
        # A record without a cell gets no H3 stratum (null) and falls through to the chain.
        r = r.with_columns(s_h3=pl.col("motion_set") + ":h3:" + pl.col("h3_cell"))
        chain = ["s_h3", *chain]
    return r, chain


def fit_radii(records: pl.DataFrame, cfg: ConformalConfig = DEFAULT_CONFORMAL,
              h3: bool = False) -> pl.DataFrame:  # fmt: skip
    """Radii for every (model, horizon, level) at each stratum level of the chain (see _strata).
    Rows: model_id, horizon_h, level, stratum, n, n_vessels, radius_m (null when n < min_n or the
    level needs more samples than n). With `h3`, records need an `h3_cell` column (add_h3)."""
    r, chain = _strata(records, h3)
    rows = []
    for col in chain:
        for (m, h, s), g in r.group_by("model_id", "horizon_h", col, maintain_order=True):
            e = g["error_m"].to_numpy()
            nv = g["vessel_id"].n_unique() if "vessel_id" in g.columns else None
            for level in cfg.levels:
                q = conformal_quantile(e, level) if len(e) >= cfg.min_n else None
                rows.append({"model_id": m, "horizon_h": float(h), "level": level,
                             "stratum": s, "n": len(e), "n_vessels": nv,
                             "radius_m": q})  # fmt: skip
    return pl.DataFrame(rows).sort("model_id", "horizon_h", "level", "stratum")


def assign_radii(records: pl.DataFrame, radii: pl.DataFrame, level: float,
                 h3: bool = False) -> pl.DataFrame:  # fmt: skip
    """Attach to each evaluable record the radius of its most specific stratum that has one:
    columns radius_m and radius_stratum (null if no radius exists even pooled), and s_first (the
    most specific stratum it could have had; a different radius_stratum is a fallback)."""
    r, chain = _strata(records, h3)
    rad = radii.filter((pl.col("level") == level) & pl.col("radius_m").is_not_null()).select(
        "model_id", "horizon_h", "stratum", "radius_m"
    )
    tags = [f"r_{i}" for i in range(len(chain))]
    for col, tag in zip(chain, tags, strict=True):
        r = r.join(rad.rename({"stratum": col, "radius_m": tag}),
                   on=["model_id", "horizon_h", col], how="left")  # fmt: skip
    which = pl.when(pl.col(tags[0]).is_not_null()).then(pl.col(chain[0]))
    for col, tag in zip(chain[1:], tags[1:], strict=True):
        which = which.when(pl.col(tag).is_not_null()).then(pl.col(col))
    drop = [*tags, *(c for c in chain if c not in ("s_class",))]
    return r.with_columns(
        radius_m=pl.coalesce(tags), radius_stratum=which, s_first=pl.col(chain[0])
    ).drop(drop)


def _vessel_ci(covered: np.ndarray, vessels: np.ndarray, n_boot: int, seed: int):
    """95% CI of a coverage share, resampling vessels (cutoffs of one vessel are correlated)."""
    codes, inv = np.unique(vessels, return_inverse=True)
    hits = np.bincount(inv, weights=covered.astype(float), minlength=len(codes))
    tot = np.bincount(inv, minlength=len(codes)).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(codes), size=(n_boot, len(codes)))
    shares = hits[idx].sum(1) / tot[idx].sum(1)
    return float(np.quantile(shares, 0.025)), float(np.quantile(shares, 0.975))


def failures_by_cell(records: pl.DataFrame) -> pl.DataFrame:
    """Records with truth where the model produced no forecast (reason model_failed), per
    (model, horizon): conformal coverage is over forecasts the model made, so these are reported
    beside it rather than silently dropped."""
    return (records.filter(pl.col("reason") == "model_failed")
            .group_by("model_id", "horizon_h").agg(n_model_failed=pl.len()))  # fmt: skip


def coverage_table(assigned: pl.DataFrame, level: float, group_cols: list[str],
                   cfg: ConformalConfig = DEFAULT_CONFORMAL, n_boot: int = 1000,
                   seed: int = 20230601) -> pl.DataFrame:  # fmt: skip
    """Empirical coverage of the assigned radii per (model, horizon, *group_cols): n, vessels,
    coverage with a vessel-bootstrap 95% CI, mean area (km^2, a disc of the radius), the share of
    samples whose radius came from a fallback stratum, and whether the cell is calibrated
    (|coverage - level| <= tolerance; judged only with n >= tolerance_min_n; the rule fixed
    before any test coverage was seen). Also: the samples with no radius at all (`n_no_radius`)
    and whether the vessel CI contains the level (`ci_contains_level`, descriptive)."""
    keys = ["model_id", "horizon_h", *group_cols]
    no_radius = (assigned.filter(pl.col("radius_m").is_null())
                 .group_by(keys).agg(n_no_radius=pl.len()))  # fmt: skip
    a = assigned.filter(pl.col("radius_m").is_not_null()).with_columns(
        covered=pl.col("error_m") <= pl.col("radius_m"),
        fallback=pl.col("radius_stratum") != pl.col("s_first"),
    )
    rows = []
    keys = ["model_id", "horizon_h", *group_cols]
    for k, g in a.group_by(keys, maintain_order=True):
        cov = g["covered"].to_numpy()
        lo, hi = _vessel_ci(cov, g["vessel_id"].to_numpy(), n_boot, seed)
        c = float(cov.mean())
        judged = g.height >= cfg.tolerance_min_n
        rows.append({
            **dict(zip(keys, k, strict=True)), "level": level, "n": g.height,
            "n_vessels": g["vessel_id"].n_unique(), "coverage": c, "ci_low": lo, "ci_high": hi,
            "mean_area_km2": float((math.pi * (g["radius_m"] / 1000) ** 2).mean()),
            "median_radius_km": float(g["radius_m"].median()) / 1000,
            "fallback_share": float(g["fallback"].mean()),
            "calibrated": (abs(c - level) <= cfg.tolerance) if judged else None,
            "ci_contains_level": lo <= level <= hi,
        })  # fmt: skip
    out = pl.DataFrame(rows).join(no_radius, on=keys, how="left")
    return out.with_columns(pl.col("n_no_radius").fill_null(0)).sort(keys)


# -- serving radii with a forecast -----------------------------------------------------------
def motion_state(reports: pl.DataFrame, underway_min_kn: float = 2.0,
                 stationary_max_kn: float = 0.5) -> str | None:  # fmt: skip
    """The cutoff sampler's motion rule on a history: mean SOG over the 10 min up to the last
    report (inclusive). Between the two thresholds there is no stratum (None)."""
    if not reports.height:
        return None
    t = reports["observed_at"][-1]
    sog = reports.filter(pl.col("observed_at") >= t - timedelta(minutes=10))["sog_knots"]
    sog = sog.drop_nulls()
    if not sog.len():
        return None
    m = float(sog.mean())
    return "underway" if m >= underway_min_kn else "stationary" if m < stationary_max_kn else None


MAX_STALENESS_MIN = 10.0  # declared: calibration cutoffs sit exactly on a report (0 min)
MIN_HISTORY_HOURS = 3.0  # the sampler's trailing window for its report-count rule


def calibration_like(reports: pl.DataFrame, as_of: datetime, history_hours: float, cutoff_box,
                     min_reports_3h: int = 6) -> str | None:  # fmt: skip
    """Why a forecast does not resemble the calibration samples (None if it does). The radii
    are only claimed for the population they were calibrated on: the sampler's cutoff region,
    Class A/B transceivers, at least `min_reports_3h` reports in the trailing 3 h, and an as_of
    at (or within MAX_STALENESS_MIN of) the last report, since lead times count from as_of."""
    if history_hours < MIN_HISTORY_HOURS:
        return f"history shorter than {MIN_HISTORY_HOURS:g} h"
    if not reports.height:
        return "no report in the history"
    last = reports.row(-1, named=True)
    if (as_of - last["observed_at"]).total_seconds() > MAX_STALENESS_MIN * 60:
        return f"the last report is more than {MAX_STALENESS_MIN:g} min older than as_of"
    if not cutoff_box.contains(last["latitude_deg"], last["longitude_deg"]):
        return f"the last report is outside the {cutoff_box.name} cutoff region"
    if last.get("transceiver_class") not in ("A", "B"):
        return "transceiver class is not A or B"
    n3 = reports.filter(pl.col("observed_at") > last["observed_at"] - timedelta(hours=3)).height
    if n3 < min_reports_3h:
        return f"fewer than {min_reports_3h} reports in the last 3 h"
    return None


def last_vessel_class(reports: pl.DataFrame) -> str:
    """The class on the last report with one (the benchmark strata use the cutoff row's)."""
    if "vessel_class" not in reports.columns:
        return "unknown"
    c = reports["vessel_class"].drop_nulls()
    return str(c[-1]) if c.len() else "unknown"


def region_for(params: dict, manifest, manifest_sha256: str | None, model_id: str,
               model_version: str, motion: str | None,
               vessel_class: str, as_of: datetime, target_times: list[datetime],
               level: float,
               config_sha256: str | None = None) -> tuple[dict | None, str | None]:  # fmt: skip
    """The ConformalRegion fields for one forecast, or (None, why). Refuses: radii fitted for a
    different manifest, model version or model config, a level that was not fitted, an as_of
    before the calibration labels' reach (calibration end + truth bracket: the radii would carry
    errors from after as_of), or no stratum."""
    if manifest is None or params.get("manifest_sha256") != manifest_sha256:
        return None, "conformal radii were fitted for a different split manifest"
    fitted = params.get("models", {}).get(model_id)
    if fitted is None:
        return None, f"no conformal radii for {model_id}"
    if fitted.get("model_version") != model_version:
        return None, f"conformal radii are for {model_id} {fitted.get('model_version')}"
    if config_sha256 is not None and fitted.get("config_sha256") != config_sha256:
        return None, f"conformal radii were fitted for a different {model_id} configuration"
    if level not in params["config"]["levels"]:
        return None, f"no radii at level {level}"
    cal_end = max((p.end for p in manifest.periods or [] if p.split == "calibration"), default=None)
    if cal_end is None:
        return None, "the split manifest has no calibration period"
    reach = (datetime.fromisoformat(params["label_reach_end"]) if params.get("label_reach_end")
             else cal_end + timedelta(minutes=30))  # fmt: skip
    if as_of < reach:
        return None, (f"as_of is before the calibration labels' reach ({reach:%Y-%m-%d %H:%M}): "
                      "its radii were fitted on later errors")  # fmt: skip
    if motion is None:
        return None, "no motion stratum (10-min mean SOG between 0.5 and 2 kn, or no SOG)"
    rows = {(r["horizon_h"], r["stratum"]): r for r in params["radii"]
            if r["model_id"] == model_id and r["level"] == level}  # fmt: skip
    table = {k: r["radius_m"] for k, r in rows.items()}
    horizons = sorted({h for h, _ in table})
    chain = (f"{motion}:{vessel_class}", f"{motion}:{ALL}", ALL)
    radii, strata, ns, nvs = [], [], [], []
    for t in target_times:
        lead_h = (t - as_of).total_seconds() / 3600
        h = next((x for x in horizons if abs(x - lead_h) < 1e-6), None)
        r, s = None, None
        if h is not None:
            s = next((c for c in chain if table.get((h, c)) is not None), None)
            r = table.get((h, s)) if s else None
        radii.append(r)
        strata.append(s)
        ns.append(rows[(h, s)].get("n") if s else None)
        nvs.append(rows[(h, s)].get("n_vessels") if s else None)
    if all(r is None for r in radii):
        return None, "no target time coincides with a fitted horizon (1, 2, 3, 6, 12, 24 h)"
    return {
        "nominal_level": level, "calibration_set_id": params["calibration_set_id"],
        "radii_m": radii, "strata": strata, "fitted_horizons_h": horizons,
        "calibration_n": ns, "calibration_vessels": nvs, "class_source": "backfilled",
    }, None  # fmt: skip
