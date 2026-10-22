"""Download the pinned Timestral weights into data/weights/ (Milestone 6, build plan §5 T1).

The adapter (models/timestral_adapter.py) only ever loads from this local cache, so this is
the one step that touches the network. The revision is pinned: a different checkpoint is a
different model version. Weights licence: Modified MIT (revenue cap, attribution); see
research/adapters/timestral/README.md. Never commit the weights.

    uv run --project backend python scripts/fetch_timestral.py
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from maritime_forecast.models.timestral_adapter import REPO_ID, REVISION, WEIGHTS_DIR
from maritime_forecast.provenance import git_sha


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-id", default=REPO_ID)
    ap.add_argument("--revision", default=REVISION)
    args = ap.parse_args()
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        args.repo_id, revision=args.revision, cache_dir=WEIGHTS_DIR,
        allow_patterns=["config.json", "model.safetensors", "LICENSE", "README.md"],
    )  # fmt: skip
    sidecar = WEIGHTS_DIR / f"{args.repo_id.replace('/', '__')}@{args.revision[:7]}.json"
    sidecar.write_text(json.dumps({
        "repo_id": args.repo_id, "revision": args.revision, "path": path,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"), "git_sha": git_sha(),
    }, indent=1) + "\n")  # fmt: skip
    print(f"{args.repo_id}@{args.revision[:7]} -> {path}")


if __name__ == "__main__":
    main()
