"""Provenance helpers for run sidecars."""

from __future__ import annotations

import subprocess

from maritime_forecast.config import REPO_ROOT


def git_sha() -> str:
    """Current commit SHA, suffixed with '-dirty' if the worktree has uncommitted changes."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).returncode
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
