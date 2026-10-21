"""GeoJSON export of a benchmark run (build plan Milestone 2: "exporting JSON and GeoJSON").

Run records hold each forecast's position, and the truth, at every scored horizon, not the
full grid. Features per (cutoff, model):
  * prediction  LineString from the anchor report through the predicted horizon points.
                The segments are straight chords between horizons, not the model's full path.
  * truth       MultiPoint of the evaluable truth positions, one per cutoff. Never a line:
                consecutive horizons can be hours apart and the track between them unseen.
  * anchor      Point of the cutoff report.
Properties carry the run id, protocol, model, horizons, per-horizon errors and feasibility
flags, and `paired` so a viewer can restrict itself to the comparable set. With
`paired_only`, cutoffs paired at no horizon are dropped and `error_m` is null wherever the
horizon is not paired, so no unpaired error can be aggregated from the export.
"""

from __future__ import annotations

import polars as pl

from maritime_forecast.schemas.benchmark import BenchmarkRun


def _num(x):
    return None if x is None else round(float(x), 2)


def run_geojson(
    run: BenchmarkRun,
    records: pl.DataFrame,
    cutoffs: pl.DataFrame,
    strata: set[str] | None = None,
    models: set[str] | None = None,
    paired_only: bool = False,
    limit: int | None = None,
) -> dict:
    need = {"pred_lat", "pred_lon", "truth_lat", "truth_lon"}
    if not need <= set(records.columns):
        raise ValueError(f"run {run.run_id} predates position records; re-run it to export")
    cut = cutoffs.with_columns(
        stratum_all=pl.col("motion_set") + ":ALL",
        stratum_cls=pl.col("motion_set") + ":" + pl.col("vessel_class").fill_null("unknown"),
    )
    if strata is not None:
        keep = pl.col("stratum_all").is_in(strata) | pl.col("stratum_cls").is_in(strata)
        if "underway:TRANSITING" in strata:
            keep = keep | pl.col("transiting")
        cut = cut.filter(keep)
    if limit is not None:
        cut = cut.sort("cutoff_id").head(limit)
    rec = records.filter(pl.col("cutoff_id").is_in(cut["cutoff_id"].implode()))
    if models is not None:
        rec = rec.filter(pl.col("model_id").is_in(list(models)))
    if paired_only:
        ids = rec.group_by("cutoff_id").agg(pl.col("paired").any())
        rec = rec.filter(pl.col("cutoff_id").is_in(ids.filter("paired")["cutoff_id"].implode()))
    feas_cols = [c for c in rec.columns if c.startswith("feas_")]
    base = {"run_id": run.run_id, "protocol": run.protocol.protocol, "dataset_id": run.dataset_id}
    by_cut = {r["cutoff_id"]: r for r in cut.iter_rows(named=True)}
    feats = []
    truth_done: set[int] = set()
    for (cid, model), g in rec.sort("horizon_h").group_by(
        "cutoff_id", "model_id", maintain_order=True
    ):
        c = by_cut[cid]
        a = [c["longitude_deg"], c["latitude_deg"]]
        common = {**base, "cutoff_id": cid, "vessel_id": c["vessel_id"],
                  "as_of": c["observed_at"].isoformat(), "motion_set": c["motion_set"],
                  "vessel_class": c["vessel_class"], "transiting": c["transiting"]}  # fmt: skip
        if cid not in truth_done:
            truth_done.add(cid)
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": a},
                          "properties": {**common, "kind": "anchor"}})  # fmt: skip
            t = g.filter(pl.col("truth_lat").is_not_null())
            feats.append({
                "type": "Feature",
                "geometry": {"type": "MultiPoint",
                             "coordinates": [[x, y] for x, y in zip(t["truth_lon"], t["truth_lat"],
                                                                    strict=True)]},
                "properties": {**common, "kind": "truth", "horizons_h": t["horizon_h"].to_list()},
            })  # fmt: skip
        p = g.filter(pl.col("pred_lat").is_not_null())
        props = {
            **common, "kind": "prediction", "model_id": model,
            "status": g["status"][0], "error_code": g["error_code"][0],
            "horizons_h": g["horizon_h"].to_list(), "paired": g["paired"].to_list(),
            "error_m": [
                _num(x) if (p_ or not paired_only) else None
                for x, p_ in zip(g["error_m"], g["paired"], strict=True)
            ],
            **{c: g[c].to_list() for c in feas_cols},
            "geometry_note": "straight chords between horizon points, not the full model path",
        }  # fmt: skip
        geom = None
        if p.height:
            coords = [a] + [[x, y] for x, y in zip(p["pred_lon"], p["pred_lat"], strict=True)]
            geom = {"type": "LineString", "coordinates": coords}
        feats.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats,
            "properties": {**base, "git_sha": run.git_sha,
                           "feasibility": run.feasibility, "n_cutoffs": len(by_cut)}}  # fmt: skip


def write_run_geojson(
    runs,
    run_id: str,
    strata: list[str] | None = None,
    models: list[str] | None = None,
    paired_only: bool = False,
    limit: int | None = None,
    name: str | None = None,
    root=None,
):
    """Write data/runs/exports/<run_id>/<name>.geojson and its sidecar; returns the path."""
    import hashlib
    import json

    from maritime_forecast.config import RUNS_DIR
    from maritime_forecast.provenance import git_sha

    run = runs.load(run_id)  # verifies checksums
    fc = run_geojson(
        run, runs.table(run_id, "records"), runs.table(run_id, "cutoffs"),
        set(strata) if strata else None, set(models) if models else None, paired_only, limit,
    )  # fmt: skip
    filt = {"strata": strata, "models": models, "paired_only": paired_only, "limit": limit}
    name = name or hashlib.sha256(json.dumps(filt, sort_keys=True).encode()).hexdigest()[:10]
    out = (root or RUNS_DIR / "exports") / run_id / f"{name}.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, separators=(",", ":")))
    digest = hashlib.sha256((runs.path(run_id) / "files.json").read_bytes()).hexdigest()
    sidecar = {"run_id": run_id, "run_files_json_sha256": digest, "filter": filt,
               "features": len(fc["features"]), "git_sha": git_sha()}  # fmt: skip
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n")
    return out
