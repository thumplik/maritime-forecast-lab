# Maritime Forecast Lab. Run `make help` for targets.
SHELL := /bin/bash
export PATH := $(HOME)/Library/Python/3.9/bin:$(HOME)/.local/bin:$(PATH)
UV  := uv
PY  := $(UV) run --project backend python
START ?= 2023-06-01
END   ?= 2023-06-30

.PHONY: help setup test lint format fetch fetch-list canonical audit calibrate notebook

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## Create/refresh the backend Python env
	cd backend && $(UV) sync

test:  ## Unit tests (no network, no local data)
	cd backend && $(UV) run pytest

lint:  ## Ruff lint + format check
	cd backend && $(UV) run ruff check src tests ../scripts && $(UV) run ruff format --check src tests ../scripts

format:  ## Apply ruff formatting and safe fixes
	cd backend && $(UV) run ruff check --fix src tests ../scripts && $(UV) run ruff format src tests ../scripts

fetch:  ## Download NOAA AIS for START..END, filtered to the truth region (resumable)
	$(PY) scripts/fetch_noaa.py --start $(START) --end $(END)

fetch-list:  ## Print the NOAA URLs for START..END without downloading
	$(PY) scripts/fetch_noaa.py --start $(START) --end $(END) --list-only

canonical:  ## Raw NOAA parquet -> canonical §4 observations
	$(PY) scripts/canonicalize.py --start $(START) --end $(END)

audit:  ## Milestone 0 coverage audit
	$(PY) scripts/coverage_audit.py

calibrate:  ## §6.6 golden-number check (needs local data)
	$(PY) scripts/calibrate.py
	cd backend && $(UV) run pytest -m golden

notebook:  ## Convert the spike notebook to .ipynb and open Jupyter
	cd backend && $(UV) run jupytext --to ipynb ../research/notebooks/m0_spike.py && $(UV) run jupyter lab ../research/notebooks
