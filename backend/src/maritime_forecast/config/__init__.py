"""Project configuration: repository paths and named geographic regions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
MANIFEST_DIR = DATA_DIR / "manifests"
RUNS_DIR = DATA_DIR / "runs"


@dataclass(frozen=True)
class BBox:
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    def sql_predicate(self, lat_col: str = "latitude_deg", lon_col: str = "longitude_deg") -> str:
        return (
            f"{lat_col} BETWEEN {self.lat_min} AND {self.lat_max} "
            f"AND {lon_col} BETWEEN {self.lon_min} AND {self.lon_max}"
        )

    def as_dict(self) -> dict[str, float | str]:
        return {
            "name": self.name,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
        }


@cache
def _regions_file() -> dict:
    with open(CONFIG_DIR / "regions.yaml") as f:
        return yaml.safe_load(f)


def region(name: str) -> BBox:
    spec = _regions_file()["regions"][name]
    return BBox(name=name, **spec)


def default_region(kind: str) -> BBox:
    """kind is 'cutoff_region' or 'truth_region'."""
    return region(_regions_file()["defaults"][kind])
