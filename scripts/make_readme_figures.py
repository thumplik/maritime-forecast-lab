"""Render the README figures (light and dark PNGs) from real runs and local data.

    docs/img/forecast-example-{light,dark}.png  one test-period cutoff: history, the real track
                                                after the cutoff, three baselines and route
                                                retrieval over 6 h,
                                                on the navigable-water mask
    docs/img/error-by-horizon-{light,dark}.png  transiting stratum, median error + 95% CI
    docs/img/on-land-{light,dark}.png           share of forecasts on infeasible land, and the
                                                mask's own error (truth on land)

Vessel identities are not shown. Needs the canonical dataset, the P-known mask and the kept
benchmark run (ids below).

Usage:
    uv run --project backend python scripts/make_readme_figures.py
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from maritime_forecast.config import MANIFEST_DIR, REPO_ROOT  # noqa: E402
from maritime_forecast.geo.navigable_mask import load_navigable_mask  # noqa: E402
from maritime_forecast.models.base import (  # noqa: E402
    ForecastHorizon,
    ModelContext,
    history_from_store,
)
from maritime_forecast.models.registry import get_model  # noqa: E402
from maritime_forecast.storage.parquet_store import ParquetStore  # noqa: E402
from maritime_forecast.storage.run_store import RunStore  # noqa: E402

DATASET = "noaa_us_gulf_2023-06-07"
RUN = "P-known_noaa_us_gulf_2023-06-07_20261021T213109Z"  # the July hold-out P-known run
# The example cutoff is chosen by a fixed rule, never by its errors: the transiting tanker
# cutoff (AIS type 80-89) nearest Bolivar Roads, the channel entrance used for the June README.
BOLIVAR_ROADS = (29.345, -94.74)
OUT = REPO_ROOT / "docs" / "img"

# Reference palette (dataviz skill), slots 1-4 in fixed order, validated in both modes (the
# light-mode contrast WARN for slots 3-4 is covered by the direct labels on every series).
THEMES = {
    "light": dict(
        surface="#fcfcfb",
        text="#0b0b0b",
        text2="#52514e",
        grid="#e4e3df",
        land="#d9d7d0",
        water="#eef3f8",
        truth="#0b0b0b",
        series=["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    ),
    "dark": dict(
        surface="#1a1a19",
        text="#f0efec",
        text2="#c3c2b7",
        grid="#3a3936",
        land="#3d3c38",
        water="#22272d",
        truth="#f0efec",
        series=["#3987e5", "#d95926", "#199e70", "#c98500"],
    ),
}
MODELS = [
    ("constant_velocity_v1", "Constant velocity"),
    ("damped_velocity_v1", "Damped velocity"),
    ("persistence_v1", "Persistence"),
    ("route_retrieval_v2", "Route retrieval"),
]


def _style(ax, th):
    ax.set_facecolor(th["surface"])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=th["text2"], labelsize=9, length=0)
    ax.grid(True, color=th["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def example_data():
    store = ParquetStore()
    runs = RunStore()
    cut = runs.table(RUN, "cutoffs").filter(
        pl.col("transiting") & (pl.col("vessel_class") == "tanker")
    )
    d2 = (pl.col("latitude_deg") - BOLIVAR_ROADS[0]) ** 2 + (
        (pl.col("longitude_deg") - BOLIVAR_ROADS[1]) * np.cos(np.radians(BOLIVAR_ROADS[0]))
    ) ** 2
    c = cut.with_columns(d2=d2).sort("d2", "cutoff_id").row(0, named=True)
    raw = (MANIFEST_DIR / "splits" / f"P-known__{DATASET}.json").read_bytes()
    ctx = ModelContext(
        store=store,
        dataset_id=DATASET,
        protocol="P-known",
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )
    as_of = c["observed_at"]
    hist = history_from_store(store, DATASET, c["vessel_id"], as_of, 24)
    hz = ForecastHorizon(as_of, 6, 10)
    preds = {}
    for m, _ in MODELS:
        f = get_model(m, ctx).predict(hist, hz)
        assert f.status == "ok", (m, f.error)
        preds[m] = np.array([[p.longitude_deg, p.latitude_deg] for p in f.points])
    past = hist.reports.filter(pl.col("observed_at") > as_of - timedelta(hours=2))
    future = store.track(DATASET, c["vessel_id"], as_of, as_of + timedelta(hours=6, minutes=1))
    mask, _ = load_navigable_mask("P-known", DATASET, hashlib.sha256(raw).hexdigest())
    return c, past, future, preds, mask


def _segments(df: pl.DataFrame, gap_min=30):
    """Split a track at gaps > gap_min: never draw across a coverage gap."""
    t = df["observed_at"].to_list()
    xy = np.c_[df["longitude_deg"].to_numpy(), df["latitude_deg"].to_numpy()]
    cuts = [0] + [i for i in range(1, len(t)) if t[i] - t[i - 1] > timedelta(minutes=gap_min)]
    cuts.append(len(t))
    return [xy[a:b] for a, b in zip(cuts, cuts[1:], strict=False) if b - a > 1]


def forecast_figure(th, name, data):
    c, past, future, preds, mask = data
    # Frame the real track and the damped/retrieval paths; constant velocity runs off it.
    keep = np.vstack(
        [
            preds["damped_velocity_v1"],
            preds["route_retrieval_v2"],
            past[["longitude_deg", "latitude_deg"]].to_numpy(),
            future[["longitude_deg", "latitude_deg"]].to_numpy(),
        ]
    )
    x0, y0 = keep.min(0) - 0.04
    x1, y1 = keep.max(0) + 0.04
    coslat = np.cos(np.radians(c["latitude_deg"]))
    g = mask.grid
    r0, r1 = int((y0 - g.lat0) / g.cell_deg), int((y1 - g.lat0) / g.cell_deg)
    c0, c1 = int((x0 - g.lon0) / g.cell_deg), int((x1 - g.lon0) / g.cell_deg)
    sub = mask.infeasible[r0:r1, c0:c1]
    w = 8.0
    h = min(9.0, w * (y1 - y0) / ((x1 - x0) * coslat) + 1.0)
    fig, ax = plt.subplots(figsize=(w, h), dpi=150)
    fig.patch.set_facecolor(th["surface"])
    ax.set_facecolor(th["water"])
    from matplotlib.colors import ListedColormap

    ax.imshow(
        sub,
        origin="lower",
        extent=(
            g.lon0 + c0 * g.cell_deg,
            g.lon0 + c1 * g.cell_deg,
            g.lat0 + r0 * g.cell_deg,
            g.lat0 + r1 * g.cell_deg,
        ),
        cmap=ListedColormap([th["water"], th["land"]]),
        interpolation="nearest",
        aspect=1 / coslat,
    )
    for s in _segments(past):
        ax.plot(s[:, 0], s[:, 1], color=th["text2"], lw=2, solid_capstyle="round")
    for i, s in enumerate(_segments(future)):
        ax.plot(
            s[:, 0],
            s[:, 1],
            color=th["truth"],
            lw=2.2,
            ls=(0, (1, 1.6)),
            label="What the ship actually did (withheld)" if i == 0 else None,
        )
    ft = future["observed_at"].to_list()
    for hh in (1, 3, 6):  # true position at each marked horizon (nearest report)
        t = c["observed_at"] + timedelta(hours=hh)
        k = int(np.argmin([abs((x - t).total_seconds()) for x in ft]))
        if abs((ft[k] - t).total_seconds()) <= 600:
            pt = future.row(k, named=True)
            ax.plot(
                pt["longitude_deg"],
                pt["latitude_deg"],
                "o",
                ms=7,
                mfc="none",
                mec=th["truth"],
                mew=1.6,
            )
            ax.annotate(
                f"{hh} h",
                (pt["longitude_deg"], pt["latitude_deg"]),
                xytext=(-8, 6),
                textcoords="offset points",
                ha="right",
                color=th["text"],
                fontsize=8,
            )
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1  # noqa: E731
    for (m, label), col in zip(MODELS, th["series"], strict=True):
        p = np.vstack([[c["longitude_deg"], c["latitude_deg"]], preds[m]])
        legend = label + (" (stays at the cutoff)" if m == "persistence_v1" else "")
        ax.plot(p[:, 0], p[:, 1], color=col, lw=2, label=legend)
        for hh in (1, 3, 6):
            ax.plot(*preds[m][hh * 6 - 1], "o", ms=6, color=col, mec=th["surface"], mew=1.5)
        last = [q for q in p if inside(q)][-1]
        off = "  (continues off map)" if not inside(p[-1]) else ""
        ax.annotate(
            label + off,
            last,
            xytext=(8, -14) if m == "route_retrieval_v2" else (8, -4 if off else -2),
            textcoords="offset points",
            color=th["text"],
            fontsize=9,
            va="top" if off else "center",
        )
    ax.plot(
        c["longitude_deg"],
        c["latitude_deg"],
        "s",
        ms=8,
        color=th["text"],
        mec=th["surface"],
        mew=1.5,
        label="Cutoff: the model sees only data up to here",
    )
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    leg = ax.legend(
        loc="lower left",
        fontsize=8.5,
        frameon=True,
        facecolor=th["surface"],
        edgecolor=th["grid"],
        labelcolor=th["text"],
    )
    leg.get_frame().set_alpha(0.92)
    fig.suptitle(
        "A tanker at the Houston Ship Channel entrance: 6 h forecasts",
        color=th["text"],
        fontsize=11,
        x=0.02,
        ha="left",
    )
    fig.text(
        0.02,
        0.012,
        "Dots: 1, 3, 6 h. Hollow rings: the true position then. Shaded: land "
        "in the navigable mask\n(OSM land minus train-split traffic cells). Land © "
        "OpenStreetMap contributors (ODbL). AIS: NOAA / USCG, July 2023 held-out test.",
        color=th["text2"],
        fontsize=7,
        va="bottom",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    fig.savefig(OUT / f"forecast-example-{name}.png", facecolor=th["surface"])
    plt.close(fig)


def _cells(stratum):
    run = RunStore().load(RUN)
    return [r for r in run.results if r.stratum == stratum]


def error_figure(th, name):
    cells = _cells("underway:TRANSITING")
    fig, ax = plt.subplots(figsize=(8, 4.4), dpi=150)
    fig.patch.set_facecolor(th["surface"])
    _style(ax, th)
    for (m, label), col in zip(MODELS, th["series"], strict=True):
        rs = sorted((r for r in cells if r.model_id == m), key=lambda r: r.horizon_minutes)
        h = [r.horizon_minutes / 60 for r in rs]
        est = np.array([r.median_error_m.estimate for r in rs]) / 1000
        lo = np.array([r.median_error_m.ci_low for r in rs]) / 1000
        hi = np.array([r.median_error_m.ci_high for r in rs]) / 1000
        ax.fill_between(h, lo, hi, color=col, alpha=0.18, lw=0)
        ax.plot(h, est, color=col, lw=2, marker="o", ms=5, mec=th["surface"], mew=1.2)
        ax.annotate(
            f"{label}  {est[-1]:.0f} km",
            (h[-1], est[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=th["text"],
            fontsize=9,
        )
    ax.set_yscale("log")
    ax.set_xscale("log")
    ax.set_xticks([1, 2, 3, 6, 12, 24], ["1 h", "2 h", "3 h", "6 h", "12 h", "24 h"])
    ax.set_yticks(
        [2, 5, 10, 20, 50, 100, 200, 500], ["2", "5", "10", "20", "50", "100", "200", "500 km"]
    )
    ax.minorticks_off()
    ax.set_xlim(0.9, 60)
    ax.set_title(
        "Ships in transit: median error by horizon (P-known, July hold-out, log scales)",
        color=th["text"],
        fontsize=11,
        loc="left",
    )
    n = min(r.n_paired for r in cells)
    fig.text(
        0.01,
        0.01,
        f"Paired: every model scored on the same samples (n ≥ {n:,} per horizon); bands are "
        f"95% vessel-bootstrap CIs.\nRun {RUN}.",
        color=th["text2"],
        fontsize=7,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(OUT / f"error-by-horizon-{name}.png", facecolor=th["surface"])
    plt.close(fig)


def land_figure(th, name):
    cells = _cells("underway:ALL")
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    fig.patch.set_facecolor(th["surface"])
    _style(ax, th)
    floor = None
    ends = []
    for (m, label), col in zip(MODELS, th["series"], strict=True):
        rs = sorted((r for r in cells if r.model_id == m), key=lambda r: r.horizon_minutes)
        h = [r.horizon_minutes / 60 for r in rs]
        v = [100 * r.feasibility.land_point_rate for r in rs]
        floor = [100 * r.feasibility.truth_on_land_rate for r in rs]
        ax.plot(h, v, color=col, lw=2, marker="o", ms=5, mec=th["surface"], mew=1.2)
        ends.append((v[-1], label))
    placed: list[float] = []  # end labels at least 5 points apart, nearest to their line
    for y, label in sorted(ends):
        ly = max([y] + [p + 5 for p in placed[-1:]])
        placed.append(ly)
        ax.annotate(
            f"{label}  {y:.0f}%",
            (h[-1], y),
            xytext=(h[-1] * 1.06, ly),
            textcoords="data",
            va="center",
            color=th["text"],
            fontsize=9,
        )
    ax.plot(h, floor, color=th["text2"], lw=1.5, ls=(0, (4, 3)))
    ax.annotate(
        f"Mask error (truth on land)  {floor[-1]:.0f}%",
        (h[-1], floor[-1]),
        xytext=(6, 4),
        textcoords="offset points",
        va="bottom",
        color=th["text2"],
        fontsize=9,
    )
    ax.set_xscale("log")
    ax.set_xticks([1, 2, 3, 6, 12, 24], ["1 h", "2 h", "3 h", "6 h", "12 h", "24 h"])
    ax.minorticks_off()
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100%"])
    ax.set_xlim(0.9, 60)
    ax.set_title(
        "Underway in port: share of forecasts that put the ship on land "
        "(anywhere up to the horizon)",
        color=th["text"],
        fontsize=11,
        loc="left",
    )
    fig.text(
        0.01, 0.01, f"P-known July hold-out, paired samples. Run {RUN}.", color=th["text2"], fontsize=7
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT / f"on-land-{name}.png", facecolor=th["surface"])
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = example_data()
    for name, th in THEMES.items():
        forecast_figure(th, name, data)
        error_figure(th, name)
        land_figure(th, name)
    print(f"wrote {sorted(p.name for p in OUT.glob('*.png'))}")


if __name__ == "__main__":
    main()
