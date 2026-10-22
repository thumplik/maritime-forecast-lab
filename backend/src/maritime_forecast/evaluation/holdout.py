"""Guards for a held-out test period (docs/evaluation-protocol.md "June + July 2023").

Manifests of version `splits-v2` have an untouched test period (July 2023) that may be scored
only for a model design frozen on the dev window. The rule is enforced, not just written:

  * A benchmark on such a test split needs an explicit confirmation (`confirm_test`) and a
    clean git tree (the code that is scored is a commit).
  * Every such run is appended to a committed ledger, `data/manifests/test_access/<dataset>.json`,
    BEFORE it is scored: run id, git SHA, protocol, models with versions and config hashes, and
    `informed_by` (what earlier results shaped the models). The ledger records every look at the
    test set, including runs that fail later.
  * The same model version and config cannot be scored on the test split twice under one
    protocol unless a re-run reason is given (and recorded).
  * The API never benchmarks the test split, and refuses forecast evaluation whose target times
    reach into it (`touches_test`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.evaluation.splits import HOLDOUT_MANIFEST_VERSION
from maritime_forecast.schemas.manifest import SplitManifest


def is_holdout(manifest: SplitManifest) -> bool:
    return manifest.version == HOLDOUT_MANIFEST_VERSION


def ledger_path(dataset_id: str) -> Path:
    return MANIFEST_DIR / "test_access" / f"{dataset_id}.json"


def config_sha(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def read_ledger(dataset_id: str) -> list[dict]:
    p = ledger_path(dataset_id)
    return json.loads(p.read_text()) if p.exists() else []


def check_test_access(manifest: SplitManifest, split: str, confirm: bool, git: str,
                      models: list[dict], rerun_reason: str | None) -> None:  # fmt: skip
    """Raise ValueError unless scoring `split` under `manifest` is allowed. `models` are
    {"model_id", "model_version", "config"} as the run will record them."""
    if not is_holdout(manifest) or split != "test":
        return
    if not confirm:
        raise ValueError(
            f"{manifest.dataset_id} has a held-out test period; scoring it needs an explicit "
            "confirmation (run_benchmark.py --confirm-test) for a design frozen on dev"
        )
    if git == "unknown" or git.endswith("-dirty"):
        raise ValueError("the held-out test split is scored only from a clean, committed tree")
    seen = {
        (e["protocol"], m["model_id"], m["model_version"], m["config_sha256"])
        for e in read_ledger(manifest.dataset_id)
        for m in e["models"]
    }
    again = [m["model_id"] for m in models
             if (manifest.protocol, m["model_id"], m["model_version"], config_sha(m["config"]))
             in seen]  # fmt: skip
    if again and not rerun_reason:
        raise ValueError(
            f"{again} were already scored on this held-out test split under {manifest.protocol}; "
            "pass a re-run reason (--test-rerun-reason) to score them again"
        )


def record_test_access(manifest: SplitManifest, run_id: str, git: str, models: list[dict],
                       informed_by: list[str], rerun_reason: str | None) -> None:  # fmt: skip
    """Append this run to the dataset's test-access ledger (before it is scored)."""
    entries = read_ledger(manifest.dataset_id)
    entries.append({
        "run_id": run_id, "protocol": manifest.protocol, "git_sha": git,
        "accessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": [{"model_id": m["model_id"], "model_version": m["model_version"],
                    "config_sha256": config_sha(m["config"])} for m in models],
        "informed_by": list(informed_by), "rerun_reason": rerun_reason,
    })  # fmt: skip
    p = ledger_path(manifest.dataset_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2) + "\n")


def record_analysis(dataset_id: str, test_run_id: str, kind: str, config: dict, git: str,
                    output: str, informed_by: list[str] | None = None,
                    rerun_reason: str | None = None) -> None:  # fmt: skip
    """Record a later analysis of a held-out run's stored records (e.g. conformal coverage):
    no model is scored again, but the look is on record with its config and code version."""
    entries = read_ledger(dataset_id)
    entries.append({
        "run_id": test_run_id, "kind": kind, "protocol": None, "git_sha": git,
        "accessed_at": datetime.now(UTC).isoformat(timespec="seconds"), "models": [],
        "config_sha256": config_sha(config), "output": output,
        "informed_by": list(informed_by or []), "rerun_reason": rerun_reason,
    })  # fmt: skip
    p = ledger_path(dataset_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2) + "\n")


def touches_test(manifest: SplitManifest, start: datetime, end: datetime) -> bool:
    """Whether [start, end] reaches into a held-out test period (API evaluation guard)."""
    if not is_holdout(manifest):
        return False
    return any(p.split == "test" and start < p.end and end >= p.start
               for p in manifest.periods or [])  # fmt: skip
