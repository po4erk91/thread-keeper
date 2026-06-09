# Config consolidation: single `~/.threadkeeper/.env` via pydantic-settings

## Context

thread-keeper configuration is currently spread across four places:

- `threadkeeper/config.py` — 60 hand-rolled `os.environ.get(...)` calls, 72 distinct
  `THREADKEEPER_*` names, no validation (a bad int env throws a raw `ValueError` at import).
- `~/.threadkeeper/spawn.toml` — role→agent/model routing, TOML.
- `~/.claude.json` `mcpServers["thread-keeper"].env` — where vars are set for the live server
  (currently only `PYTHONPATH`).
- Inline env in hooks (`tk-brief.sh` sets `THREADKEEPER_BRIEF_NO_THREAD_NUDGE`, and — after the
  recent brief-footprint work — `THREADKEEPER_BRIEF_LEAN`).

There is no single source of truth. The user asked for all config "в одном месте, в виде .env"
and chose **pydantic-settings** as the mechanism (typed + validated, reads `.env` AND env with
correct precedence, removes the brittle getters).

## Goal & constraints

- One `.env`-style source of truth at `~/.threadkeeper/.env`, read via pydantic-settings.
- **No functionality loss.** Observable behavior (brief render, daemon defaults, spawn routing)
  identical when no `.env` is present and no env is set → defaults match today's values exactly.
- Real env vars still override (`.env` is lower priority) — needed for tests/CI/ad-hoc. Pydantic
  precedence `init kwargs > env > .env > field defaults` gives this for free.
- Keep the **52 `from .config import X` call sites** working unchanged (compat shim).
- **Breaking (documented):** `spawn.toml` retired; per-role spawn env keys move to the nested
  form (`THREADKEEPER_SPAWN__MODEL__CLAUDE` instead of `THREADKEEPER_SPAWN_MODEL_CLAUDE`).

## Architecture

### 1. `Settings(BaseSettings)` in `config.py`

```python
model_config = SettingsConfigDict(
    env_prefix="THREADKEEPER_",
    env_file=<resolved ~/.threadkeeper/.env, override via THREADKEEPER_ENV_FILE>,
    env_file_encoding="utf-8",
    env_nested_delimiter="__",
    case_sensitive=False,
    extra="ignore",
)
```

- ~60 typed scalar fields mirroring current knobs (`db_path: Path`, `embed_model: str`,
  `*_interval_s: float`, `*_mb: int`, nudge intervals `int`, booleans, dirs `Path`), **same
  defaults as today**.
- Two legacy unprefixed names (`CLAUDE_SKILLS_DIR`, `CLAUDE_PROJECTS_DIR`) read the bare env via
  `Field(validation_alias=AliasChoices("CLAUDE_SKILLS_DIR", ...))` so the prefix is bypassed.

### 2. Nested spawn config + `spawn_config.py` refactor

```python
class SpawnSettings(BaseModel):
    default: str = "claude"          # default agent/cli
    loop: dict[str, str] = {}        # role  -> cli
    model: dict[str, str] = {}       # cli-or-role -> model
# Settings.spawn: SpawnSettings = SpawnSettings()
```

Env (nested `__`): `THREADKEEPER_SPAWN__DEFAULT`, `THREADKEEPER_SPAWN__LOOP__<ROLE>`,
`THREADKEEPER_SPAWN__MODEL__<KEY>`.

**Why this refactor is required:** pydantic-settings reads `.env` into the Settings *object*, not
into `os.environ`. `spawn_config.py` currently resolves roles via `os.environ.get(...)`, which
would miss `.env`-only values — breaking the "single source" goal. So `resolve_agent`,
`resolve_model`, `summary_table` are refactored to read `settings.spawn`. Remove `_load_file`,
`_config_path`, the `tomllib` import, and `THREADKEEPER_SPAWN_CONFIG`. Resolution semantics
unchanged: per-role → `spawn.loop`/`spawn.model` → `spawn.default` → active CLI → `"claude"`.

### 3. Compat shim (don't touch the 52 import sites)

After `settings = Settings()`, re-export the prior module-level names:
`DB_PATH = settings.db_path`, `MEMORY_NUDGE_INTERVAL = settings.memory_nudge_interval`,
`BRIEF_LEAN = settings.brief_lean`, … Derived constants computed after instantiation and kept
module-level: `SEMANTIC_AVAILABLE` (from `no_embeddings` + backend + `_installed()`),
`FASTEMBED_MODEL_ID`, `BACKGROUND_DAEMONS_ALLOWED`. The legacy DB-path migration side-effect
(`~/.memory_partner` → `~/.threadkeeper`) is preserved, running after Settings using
`settings.db_path`.

### 4. Process-scoped vars

`brief_lean`, `brief_no_thread_nudge`, `spawned_child`, `write_origin` become Settings fields with
safe defaults. They are still set by the hook / `spawn()` via **real env** (which beats `.env`),
so the server-process defaults hold. `THREADKEEPER_BRIEF_LEAN` must NOT go in the shared `.env`
(it would make the server's agent-facing `brief()` lean too) — documented as hook-inline-only.

### 5. `.env.example` (committed) + `.env` (gitignored)

- A small generator (`scripts/gen_env_example.py`) derives `.env.example` from the Settings schema
  (field name → `THREADKEEPER_<NAME>=<default>` + the field description as a comment), grouped by
  subsystem, so it can't drift. A test asserts every Settings field appears in `.env.example`.
- Add `.env` to `.gitignore`.
- Installer (`_setup.py` / `install.sh`): copy `.env.example` → `~/.threadkeeper/.env` if absent;
  stop creating / referencing `spawn.toml`.

### 6. Dependency

Add `pydantic>=2` + `pydantic-settings` to `pyproject.toml`. Imported in every process including
slim spawned children (`THREADKEEPER_NO_EMBEDDINGS`); accepted tradeoff — pydantic-core is
compiled, import is fast (not torch-scale).

## Migration

One-shot: read the user's existing `~/.threadkeeper/spawn.toml` and write the equivalent keys into
`~/.threadkeeper/.env`:

```
THREADKEEPER_SPAWN__DEFAULT=claude
THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=claude
THREADKEEPER_SPAWN__LOOP__ARCHIVIST=claude
THREADKEEPER_SPAWN__LOOP__CURATOR=claude
THREADKEEPER_SPAWN__LOOP__CANDIDATE_REVIEWER=claude
THREADKEEPER_SPAWN__LOOP__EXTRACT=claude
THREADKEEPER_SPAWN__LOOP__EVOLVE_REVIEWER=claude
THREADKEEPER_SPAWN__LOOP__PROBE_RUNNER=claude
THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet
```

Then delete `spawn.toml` (support removed).

## Docs (same change-set)

README (config section), `docs/ARCHITECTURE.md` (config + spawn sections), CONTRIBUTING (config
knobs), CHANGELOG (breaking: spawn.toml retired; config now pydantic-settings + `.env`).

## Testing

- **Settings:** defaults equal prior values (golden compare against a frozen snapshot); `.env` is
  read; **real env overrides `.env`**; a bad-type value yields a clear validation error; missing
  `.env` → defaults, no error; `THREADKEEPER_ENV_FILE` override honored.
- **CLAUDE_\* aliases** read the bare env names.
- **Spawn resolution:** parity table — per-role from `settings.spawn.loop`/`.model`, `default`,
  active-CLI fallback, `"claude"` last — matches pre-refactor behavior.
- **`.env.example` generator** stays in sync (test: every field present).
- **Regression:** full suite green (exercises the 52 import sites through the shim).

## Open items (resolve during implementation)

- Confirm dict population via `env_nested_delimiter` (`THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet`
  → `spawn.model["claude"]="sonnet"`). Fallback: JSON-valued env (`THREADKEEPER_SPAWN__MODEL=
  '{"claude":"sonnet"}'`).
- `env_file` override mechanism: class-time resolution of `THREADKEEPER_ENV_FILE` vs
  `Settings(_env_file=...)`.
- `validation_alias` + `env_prefix` interaction for the `CLAUDE_*` names.

## Out of scope

- Migrating the runtime dir to XDG `~/.config/thread-keeper/` (keep `~/.threadkeeper/` for
  consistency with the existing db/hooks/skills/state layout).
- Changing `~/.claude.json` beyond confirming it keeps only `command` + `PYTHONPATH` (bootstrap
  needed to import the package before `.env` is read).
