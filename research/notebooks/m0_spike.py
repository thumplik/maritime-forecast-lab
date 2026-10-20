# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#   kernelspec:
#     display_name: Python 3
#     name: python3
# ---

# %% [markdown]
# # Milestone 0 spike — coverage audit and baselines
#
# Reads the latest `coverage_audit_*` and `calibration_*` runs written by
# `scripts/coverage_audit.py` and `scripts/calibrate.py`. Numbers here are not recomputed;
# this notebook only visualises those runs. Open with `make notebook`.

# %%
import json

import matplotlib.pyplot as plt
import polars as pl

from maritime_forecast.config import RUNS_DIR

audit_dir = sorted(RUNS_DIR.glob("coverage_audit_*"))[-1]
cal_dir = sorted(RUNS_DIR.glob("calibration_*"))[-1]
audit = json.loads((audit_dir / "summary.json").read_text())
cal = json.loads((cal_dir / "summary.json").read_text())
recs = pl.read_parquet(audit_dir / "records.parquet")
print(audit_dir.name, cal_dir.name, recs.shape)

# %% [markdown]
# ## Ground-truth coverage by horizon
# Gulf-wide truth vs truth restricted to the Houston cutoff box.

# %%
cov = pl.DataFrame(audit["coverage"])
fig, ax = plt.subplots(figsize=(7, 4))
for ms, g in cov.group_by("motion_set"):
    ax.plot(g["horizon_h"], g["frac_truth_region"], "o-", label=f"{ms[0]} – Gulf truth")
    ax.plot(g["horizon_h"], g["frac_cutoff_region_only"], "o--", label=f"{ms[0]} – box only")
ax.axhline(0.5, color="grey", lw=0.8, ls=":")
ax.set(xscale="log", xlabel="horizon (h)", ylabel="fraction with valid truth", ylim=(0, 1))
ax.set_xticks(audit["horizons_h"], audit["horizons_h"])
ax.legend()

# %% [markdown]
# ## Persistence vs constant velocity, underway cutoffs, by class

# %%
uw = recs.filter(pl.col("evaluable") & (pl.col("motion_set") == "underway"))
med = (
    uw.group_by("vessel_class", "horizon_h")
    .agg(
        pl.len(),
        (pl.col("err_persistence_m").median() / 1000).alias("persistence"),
        (pl.col("err_cv_m").median() / 1000).alias("cv"),
    )
    .filter(pl.col("len") >= 50)
    .sort("vessel_class", "horizon_h")
)
classes = med["vessel_class"].unique().sort().to_list()
fig, axes = plt.subplots(2, (len(classes) + 1) // 2, figsize=(14, 6), sharex=True)
for ax, cls in zip(axes.flat, classes, strict=False):
    g = med.filter(pl.col("vessel_class") == cls)
    ax.plot(g["horizon_h"], g["persistence"], "o-", label="persistence")
    ax.plot(g["horizon_h"], g["cv"], "s-", label="CV")
    ax.set(title=cls, xscale="log", yscale="log")
axes.flat[0].legend()
fig.supxlabel("horizon (h)")
fig.supylabel("median error (km)")
fig.tight_layout()

# %% [markdown]
# ## Calibration variants vs golden CV numbers

# %%
var = pl.DataFrame(
    [
        {k: v for k, v in r.items() if not k.endswith("h")}
        | {f"{h}h": r[f"{h}h"][0] for h in (1, 2, 3)}
        for r in cal["variants"]
    ]
)
var.filter(pl.col("stat") == "mean").select(
    "jitter", "cv_mode", "vessels", "n_windows", "1h", "2h", "3h", "within_tolerance", "primary"
)
