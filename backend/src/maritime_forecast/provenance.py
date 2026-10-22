"""Provenance helpers for run sidecars."""

from __future__ import annotations

import subprocess

from maritime_forecast.config import REPO_ROOT

CODE_PATHS = ("backend", "scripts", "config", "data/model_params")


def git_sha() -> str:
    """Current commit SHA, suffixed with '-dirty' if the worktree has uncommitted changes to
    tracked files, or untracked files under the code and parameter paths."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).returncode
        # Untracked code or parameters also make the tree dirty (git diff ignores them): a run
        # stamped with a SHA must be reproducible from that commit alone.
        untracked = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHS],
            cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        dirty = dirty or any(line.startswith("??") for line in untracked.splitlines())
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
