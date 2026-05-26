# ONNX embedding backend — design

**Date:** 2026-05-26
**Status:** approved (pending spec review)
**Branch:** `feat/onnx-embeddings`

## Problem

The main `threadkeeper.server` process holds a **~1.8 GB physical footprint**
(peak 1.9 GB; RSS observed climbing past 1.1 GB). Root cause: embeddings are
computed via `sentence-transformers`, which loads the full **PyTorch CPU
stack** (`libtorch_cpu.dylib` et al.) plus `transformers`, `scikit-learn`, and
`scipy` — all to run a single 384-dim, ~33M-param model
(`paraphrase-multilingual-MiniLM-L12-v2`, 118 MB).

On-disk weight of the stack pulled into the process:

| Package | Size | Why it's loaded |
|---|---|---|
| torch | 408 MB | only to run MiniLM |
| transformers | 101 MB | sentence-transformers dep |
| scipy | 98 MB | transitive via sklearn |
| sklearn | 46 MB | sentence-transformers dep |
| numpy | 33 MB | used directly |
| sentence_transformers | 4.8 MB | the wrapper |
| **model** | **118 MB** | the only thing doing real work |

`memory_guard` can drop the model weights on an RSS threshold
(`reclaim_memory → unload_model`), but the torch dylibs stay mapped for the
life of the process, so steady state never falls far.

Swap is 0 because macOS *compresses* these dirty pages rather than paging to
disk — the footprint is real RAM pressure, just not visible as swap.

## Goal

Replace the embedding runtime with **ONNX Runtime via `fastembed`** (pure
onnxruntime + tokenizers, no torch/transformers/sklearn/scipy), keeping the
same model and 384-dim output. Target footprint **~250–400 MB**, disk savings
**~650 MB**.

## Feasibility (verified 2026-05-26)

venv runs **Python 3.14.2**. `pip install --dry-run "fastembed>=0.3"` resolves
cleanly to **fastembed 0.8.0 + onnxruntime 1.26.0** (cp314 wheels exist), plus
small deps (mmh3, py_rust_stemmers, pillow, protobuf, flatbuffers, loguru).
No torch in the dependency tree. Migration is feasible on the current
interpreter.

## Non-goals

- Changing the embedding model or vector dimension (stays
  `paraphrase-multilingual-MiniLM-L12-v2`, 384-dim).
- Changing the `vec0` schema, `sqlite-vec` usage, or the public MCP API.
- Touching `unload_model()` / `model_loaded()` / `memory_guard` contracts.
- Re-architecting `search_via_parent` delegation or the `NO_EMBEDDINGS`
  opt-out.

## Decisions

1. **Backend:** `fastembed` (ONNX) is the default. `sentence-transformers`
   remains an opt-in fallback, selected by env. Default
   `THREADKEEPER_EMBED_BACKEND=onnx`; alternative `sentence-transformers`.
2. **Recompute:** the existing **152,315 dialog + 344 note vectors** were
   produced by sentence-transformers. fastembed's output for this model is
   numerically *not identical* (quantization + pooling detail — qdrant/fastembed
   issue #368), so mixing stored ST vectors with ONNX queries lives in slightly
   different spaces. We do a **full one-shot recompute** (`tk-migrate-embeddings
   --all`) at migration time to homogenize the space immediately.
3. **Per-row backend tag:** add `embed_backend` to `notes` and
   `dialog_messages` so future backend switches / partial writes can self-heal,
   and so the migration command knows what is stale. Safety net beyond the
   one-shot recompute.

## Architecture

### `config.py`

- New `EMBED_BACKEND` constant from `THREADKEEPER_EMBED_BACKEND`
  (default `onnx`; accepts `onnx` | `sentence-transformers`).
- `SEMANTIC_AVAILABLE` probe becomes backend-aware:
  - `onnx` → try importing `fastembed` (+ numpy).
  - `sentence-transformers` → existing import probe.
  - `NO_EMBEDDINGS` short-circuit unchanged.
- Keep `EMBED_MODEL_NAME` as the canonical short name; backends map it to
  their own id (fastembed wants `sentence-transformers/<name>`).

### `embeddings.py`

- `_get_model()` branches on `EMBED_BACKEND`:
  - `onnx`: `from fastembed import TextEmbedding; TextEmbedding(model_name=<qualified>)`.
  - `sentence-transformers`: unchanged path.
- `encode(text)` returns a 384-dim float32, **explicitly L2-normalized**
  (fastembed does not guarantee unit vectors the way
  `SentenceTransformer.encode(normalize_embeddings=True)` does). Dot product on
  unit vectors == cosine, preserving the existing `vec0` / BLOB search math.
- A small batch helper `encode_many(texts)` for the migration command (fastembed
  is generator-based and far faster batched).
- `unload_model()` continues to drop the cached object; for fastembed there is
  no torch to release, but clearing the reference is still correct.

### Schema (`db.py`)

- `ALTER TABLE notes ADD COLUMN embed_backend TEXT` and likewise for
  `dialog_messages`, guarded (idempotent; ignore "duplicate column").
- `NULL` = legacy (sentence-transformers). New/recomputed rows tagged with the
  producing backend.
- `vec0` virtual tables and `EMBED_DIM` (384) unchanged.

### Migration command `tk-migrate-embeddings`

- Console entry point (added to `pyproject.toml [project.scripts]`), or
  `python -m threadkeeper.migrate_embeddings`.
- Flags:
  - `--all` — recompute every row whose `embed_backend` differs from the active
    backend (covers NULL legacy rows).
  - `--notes-only` / `--dialog-only` — scope limiters.
  - `--batch N` (default 256), `--dry-run`.
- Behavior: stream rows in batches, `encode_many`, rewrite both the BLOB column
  and the `vec0` row, set `embed_backend`, commit per batch. **Resumable**
  (re-running skips already-tagged rows) and **idempotent**. Progress logged
  every batch with ETA.

### Lazy self-heal (secondary)

- On result hydration in semantic search, if a returned row's `embed_backend`
  differs from active and its text is in hand, re-encode + persist. Keeps the
  space consistent for any rows written before a future backend change. After
  the one-shot `--all`, this path is rarely hit.

### `pyproject.toml`

```toml
[project.optional-dependencies]
semantic     = ["fastembed>=0.3", "numpy>=1.24.0", "sqlite-vec>=0.1.9"]
semantic-st  = ["sentence-transformers>=2.2.0", "numpy>=1.24.0", "sqlite-vec>=0.1.9"]
```

`semantic` (default recommended extra) no longer pulls torch. Users wanting the
old runtime install `.[semantic-st]` and set
`THREADKEEPER_EMBED_BACKEND=sentence-transformers`.

## Error handling / compatibility

- If `EMBED_BACKEND=onnx` but `fastembed` is missing → `SEMANTIC_AVAILABLE=False`
  with a clear one-line log pointing at `pip install .[semantic]`; brief + FTS
  fallback still work (existing behavior).
- First fastembed run downloads the ONNX model to its cache; document this and
  the cache location.
- The legacy `memory_partner` DB auto-migration in `config.py` is untouched.

## Testing

- Unit: `encode()` returns shape (384,), L2-norm ≈ 1.0, dtype float32 — for
  whichever backend is installed (skip if neither).
- Backend-switch: monkeypatch `EMBED_BACKEND`, assert `_get_model()` picks the
  right loader; assert graceful `SEMANTIC_AVAILABLE=False` when the lib is
  absent.
- Migration: seed a tmp DB with a few NULL-backend rows, run migrate, assert all
  rows tagged + vec0 rebuilt + re-run is a no-op (idempotent/resumable).
- Existing semantic search tests pass unchanged against ONNX vectors.

## Rollout

1. Land code + `semantic` extra change on `feat/onnx-embeddings`.
2. `pip install -e .[semantic]` (brings fastembed; torch can be uninstalled).
3. Run **`tk-migrate-embeddings --all`** to homogenize the 152k+344 vectors.
4. Restart the server; verify footprint dropped and `dialog_search` quality holds.
5. Update docs (README, ARCHITECTURE.md, RELEASING.md if release-relevant) in the
   same change-set. PR into protected `main`.

## Docs to update (same change-set)

- `README.md` — backend env switch, `semantic` vs `semantic-st` extras, the
  migration command, expected footprint.
- `ARCHITECTURE.md` — embeddings layer now backend-pluggable; ONNX default.
- `ROADMAP.md` — mark if this was a tracked item.
