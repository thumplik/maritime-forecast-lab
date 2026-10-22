# Maritime Forecast Lab. Run `make help` for targets.
SHELL := /bin/bash
export PATH := $(HOME)/.local/node/bin:$(HOME)/Library/Python/3.9/bin:$(HOME)/.local/bin:$(PATH)
UV  := uv
PY  := $(UV) run --project backend python
START ?= 2023-06-01
END   ?= 2023-06-30
# Dataset for splits/fit/mask/benchmark: the June dataset, or noaa_us_gulf_2023-06-07 (June +
# July: dev window and the fresh July test). SPLIT=dev benchmarks the dev window.
DS    ?= noaa_us_gulf_2023-06
SPLIT ?= test

NPM := npm --prefix frontend

.PHONY: help setup dev web-test e2e gen-api timestral-weights test lint format fetch fetch-list canonical audit calibrate splits import fetch-dma api benchmark fit notebook fetch-land mask index

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## Create/refresh the backend Python env and frontend node_modules
	cd backend && $(UV) sync
	$(NPM) ci --no-audit --no-fund

dev:  ## Start API (:8000) and web app (:3000) together
	./scripts/dev.sh

web-test:  ## Frontend typecheck + unit tests
	$(NPM) run typecheck && $(NPM) test

e2e:  ## Playwright smoke test on the SYNTHETIC fixture (API :8100 + web :3100; runs beside make dev)
	cd frontend && npx playwright test

gen-api:  ## Regenerate OpenAPI JSON and TypeScript types after API changes
	$(PY) scripts/export_openapi.py && $(NPM) run gen:api

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

fetch-dma:  ## Download DMA days: make fetch-dma DAYS="2023-06-01 2023-06-02"
	$(PY) scripts/fetch_dma.py $(DAYS)

import:  ## Import other-provider AIS files: make import MAPPING=dma_denmark DATASET=id FILES="a.csv"
	$(PY) scripts/import_ais.py --mapping $(MAPPING) --dataset-id $(DATASET) $(FILES)

splits:  ## Materialize split manifests into data/manifests/splits/
	$(PY) scripts/make_splits.py --dataset-id $(DS)

fit:  ## Fit damped-velocity tau on a protocol's train split: make fit PROTOCOL=P-known
	$(PY) scripts/fit_damped_velocity.py --protocol $(PROTOCOL) --dataset-id $(DS)

fetch-land:  ## OSM land polygons for the truth region (~0.9 GB transfer, deleted after filtering)
	$(PY) scripts/fetch_land_polygons.py

mask:  ## Navigable-water mask from TRAIN-split traffic: make mask PROTOCOL=P-known
	$(PY) scripts/build_navigable_mask.py --protocol $(PROTOCOL) --dataset-id $(DS)

index:  ## Retrieval segment index (completed segments + regular tracks) for the June dataset
	$(PY) scripts/build_segment_index.py

timestral-weights:  ## Download the pinned Timestral-28M weights to data/weights/ (needs `uv sync --extra timestral`)
	uv run --project backend python scripts/fetch_timestral.py

benchmark:  ## Paired benchmark: make benchmark PROTOCOL=P-known [DS=noaa_us_gulf_2023-06-07 SPLIT=dev]
	$(PY) scripts/run_benchmark.py --protocol $(PROTOCOL) --dataset-id $(DS) --split $(SPLIT)

api:  ## Run the read API on http://localhost:8000 (docs at /docs)
	cd backend && $(UV) run uvicorn maritime_forecast.api.app:app --reload --port 8000

notebook:  ## Convert the spike notebook to .ipynb and open Jupyter
	cd backend && $(UV) run jupytext --to ipynb ../research/notebooks/m0_spike.py && $(UV) run jupyter lab ../research/notebooks
