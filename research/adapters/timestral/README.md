# Timestral (Milestone 6, build plan §5 T1)

Third-party model used **unmodified, as an installed package**. No upstream code is vendored here; this file is the provenance record.

| | |
|---|---|
| Upstream code | https://github.com/mistralai/timestral (Apache-2.0) |
| Pinned commit | `a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff` ("Timestral release", 2026-10-15; the only commit when pinned). Installed as `timestral 1.0.0` |
| Install | `cd backend && uv sync --extra timestral` (optional extra in `backend/pyproject.toml`; never installed by CI or `make setup`). Brings torch 2.14.1 |
| Checkpoint | `mistralai/Timestral-28M` on Hugging Face, revision `49b26409ac87b5935539a5d8ffe7f397b720db47` (last modified 2026-10-19). Fetch with `make timestral-weights` into `data/weights/huggingface/` (gitignored, never committed) |
| Weights licence | "Modified MIT". Grants MIT-style rights **only if your organisation's global consolidated revenue for the preceding month is at most US$20M**; above that, a commercial licence from Mistral is needed. Attribution and permission notices must accompany copies. Checked 2026-10-22 for this research workbench; re-check before any other use |
| Adapter | `backend/src/maritime_forecast/models/timestral_adapter.py` (`timestral_pos_v1`) |

## Facts that constrain the adapter (from the package source at the pinned commit)

- Input `(B, V, T)`; NaN marks missing values. Patches of 32 steps; context capped at 32,768 steps (most recent kept).
- Up to 1,024 forecast steps per block; longer horizons decode block by block with the median fed back. The API caps a request at 1,024 target steps, so the adapter always runs a single block.
- Output: 21 quantiles (0.01 … 0.99) per variate, with `.median`.
- `forecast(..., clip_nonnegative=True)` is the **default** and clamps a series at 0 whenever its observed context is non-negative. In a local frame centred on the last report, that is every track heading south-west, which would pin the forecast to the anchor. The adapter passes `clip_nonnegative=False`.
- `symmetric_averaging=True` (default) averages the forecast with the mirrored forecast of `-context`. Kept: the sign of a position in the local frame is arbitrary.
- `from_pretrained(..., local_files_only=True)`: predicting never touches the network.

## Modifications

None to upstream code. All adaptation (frame, context grid, missing values, sign handling) is in the adapter and documented in `docs/model-integration-notes.md`.
