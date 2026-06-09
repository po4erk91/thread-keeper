# Single-`.env` config via pydantic-settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate all thread-keeper config (60 `THREADKEEPER_*` knobs + spawn.toml) into one `~/.threadkeeper/.env`, read through a typed pydantic-settings `Settings` object, with default behavior byte-identical and the 52 `from .config import X` call sites untouched.

**Architecture:** `config.py` defines `Settings(BaseSettings)` (env_prefix, env_file, `env_nested_delimiter="__"`) + a nested `SpawnSettings`; instantiates `settings = Settings()`; re-exports the prior module-level names as a compat shim; computes derived constants + runs the DB-migration side-effect after instantiation. `spawn_config.py` is refactored to read `settings.spawn` instead of `os.environ`/`tomllib`. spawn.toml is retired.

**Tech Stack:** Python 3.11+, pydantic 2.13, pydantic-settings 2.14 (both already in the venv).

**Spec:** `docs/superpowers/specs/2026-06-09-config-single-env-pydantic-settings-design.md`

**Spike results (already verified — bake these in, no fallbacks needed):**
- `env_nested_delimiter="__"` populates `dict[str,str]` fields: `THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet` → `spawn.model == {"claude":"sonnet"}`. With `case_sensitive=False`, **dict keys are lowercased** (aligns with spawn_config's case-insensitive role/cli handling).
- Precedence confirmed: real env (99) overrides `.env` (7) overrides default.
- `Field(validation_alias=AliasChoices("CLAUDE_SKILLS_DIR", ...))` reads the bare, unprefixed env name.
- Bad type → `pydantic.ValidationError` (clear), not raw `ValueError`.

---

### Task 1: Add pydantic-settings to dependencies

**Files:**
- Modify: `pyproject.toml` (dependencies array)

- [ ] **Step 1: Add deps.** In `[project] dependencies`, add `"pydantic>=2"` and `"pydantic-settings>=2"` (keep alphabetical/grouping consistent with the file).
- [ ] **Step 2: Verify import.** Run: `.venv/bin/python -c "import pydantic, pydantic_settings; print(pydantic.VERSION, pydantic_settings.__version__)"` → expect `2.13.x 2.14.x`.
- [ ] **Step 3: Commit.** `git add pyproject.toml && git commit -m "build: add pydantic-settings dependency"`

---

### Task 2: `Settings` + `SpawnSettings` + compat shim in config.py

**Files:**
- Modify: `threadkeeper/config.py` (full rewrite of the body; preserve all exported names + the DB-migration side-effect)
- Test: `tests/test_config_settings.py` (create)

**Reference:** the CURRENT `threadkeeper/config.py` is the source of truth for field names + defaults. Mirror **every** `os.environ.get(...)`/derived constant as a typed field or post-init constant with the **identical default**. There are ~60 scalars + 2 unprefixed (`CLAUDE_SKILLS_DIR`, `CLAUDE_PROJECTS_DIR`) + the derived trio.

- [ ] **Step 1: Write failing tests** in `tests/test_config_settings.py`:

```python
import importlib, os, tempfile, pytest

def _fresh_config(monkeypatch, env=None, env_file=None):
    for k in list(os.environ):
        if k.startswith("THREADKEEPER_") or k in ("CLAUDE_SKILLS_DIR", "CLAUDE_PROJECTS_DIR"):
            monkeypatch.delenv(k, raising=False)
    if env_file:
        monkeypatch.setenv("THREADKEEPER_ENV_FILE", env_file)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    import threadkeeper.config as c
    return importlib.reload(c)

def test_defaults_match(monkeypatch):
    c = _fresh_config(monkeypatch)
    assert c.MEMORY_NUDGE_INTERVAL == 10
    assert c.SKILL_NUDGE_INTERVAL == 10
    assert c.BRIEF_LEAN is False
    assert c.SPAWN_BUDGET_MB == 3072
    assert str(c.DB_PATH).endswith("/.threadkeeper/db.sqlite")

def test_env_overrides_default(monkeypatch):
    c = _fresh_config(monkeypatch, env={"THREADKEEPER_MEMORY_NUDGE_INTERVAL": "3"})
    assert c.MEMORY_NUDGE_INTERVAL == 3

def test_dotenv_read_and_env_wins(monkeypatch):
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("THREADKEEPER_MEMORY_NUDGE_INTERVAL=7\n")
        path = f.name
    c = _fresh_config(monkeypatch, env_file=path)
    assert c.MEMORY_NUDGE_INTERVAL == 7  # from .env
    c2 = _fresh_config(monkeypatch, env={"THREADKEEPER_MEMORY_NUDGE_INTERVAL": "99"}, env_file=path)
    assert c2.MEMORY_NUDGE_INTERVAL == 99  # real env beats .env

def test_claude_dir_bare_alias(monkeypatch):
    c = _fresh_config(monkeypatch, env={"CLAUDE_SKILLS_DIR": "/tmp/x"})
    assert str(c.CLAUDE_SKILLS_DIR) == "/tmp/x"

def test_bad_type_raises(monkeypatch):
    with pytest.raises(Exception):
        _fresh_config(monkeypatch, env={"THREADKEEPER_MEMORY_NUDGE_INTERVAL": "nope"})
```

- [ ] **Step 2: Run, verify fail.** `.venv/bin/python -m pytest tests/test_config_settings.py -q` → FAIL (no Settings yet).

- [ ] **Step 3: Implement** the new `config.py`. Structure (complete pattern; mirror remaining fields from the old file):

```python
from __future__ import annotations
import importlib.util, os
from pathlib import Path
from pydantic import Field, AliasChoices, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel

_ENV_FILE = os.environ.get("THREADKEEPER_ENV_FILE", str(Path("~/.threadkeeper/.env").expanduser()))

class SpawnSettings(BaseModel):
    default: str = "claude"
    loop: dict[str, str] = {}     # role -> cli  (keys lowercased)
    model: dict[str, str] = {}    # cli/role -> model

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THREADKEEPER_", env_file=_ENV_FILE, env_file_encoding="utf-8",
        env_nested_delimiter="__", case_sensitive=False, extra="ignore",
    )
    # paths
    db: Path = Field(Path("~/.threadkeeper/db.sqlite"), validation_alias=AliasChoices("THREADKEEPER_DB"))
    claude_skills_dir: Path = Field(Path("~/.claude/skills"),
        validation_alias=AliasChoices("CLAUDE_SKILLS_DIR", "THREADKEEPER_CLAUDE_SKILLS_DIR"))
    claude_projects_dir: Path = Field(Path("~/.claude/projects"),
        validation_alias=AliasChoices("CLAUDE_PROJECTS_DIR", "THREADKEEPER_CLAUDE_PROJECTS_DIR"))
    # embeddings
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"   # was THREADKEEPER_EMBED_MODEL
    embed_backend: str = "onnx"
    no_embeddings: bool = False
    # nudges / brief
    memory_nudge_interval: int = 10
    skill_nudge_interval: int = 10
    brief_lean: bool = False
    brief_no_thread_nudge: bool = False
    # ... mirror EVERY remaining THREADKEEPER_* knob from the old config.py here,
    #     same name (lowercased, prefix stripped) and same default + type ...
    spawn: SpawnSettings = SpawnSettings()

    @field_validator("db", "claude_skills_dir", "claude_projects_dir", mode="after")
    @classmethod
    def _expand(cls, p: Path) -> Path:
        return p.expanduser()

settings = Settings()

# ---- compat shim: keep every name the package imports ----
DB_PATH = settings.db
EMBED_MODEL_NAME = settings.embed_model
EMBED_BACKEND = settings.embed_backend.strip().lower()
NO_EMBEDDINGS = settings.no_embeddings
MEMORY_NUDGE_INTERVAL = settings.memory_nudge_interval
SKILL_NUDGE_INTERVAL = settings.skill_nudge_interval
BRIEF_LEAN = settings.brief_lean
CLAUDE_SKILLS_DIR = settings.claude_skills_dir
CLAUDE_PROJECTS_DIR = settings.claude_projects_dir
# ... one line per remaining exported name (grep the package for `config import` to enumerate) ...

# ---- derived constants (unchanged logic, now from settings) ----
FASTEMBED_MODEL_ID = EMBED_MODEL_NAME if "/" in EMBED_MODEL_NAME else f"sentence-transformers/{EMBED_MODEL_NAME}"
def _installed(*mods: str) -> bool:
    try: return all(importlib.util.find_spec(m) is not None for m in mods)
    except (ImportError, ValueError): return False
if NO_EMBEDDINGS: SEMANTIC_AVAILABLE = False
elif EMBED_BACKEND == "sentence-transformers": SEMANTIC_AVAILABLE = _installed("sentence_transformers", "numpy")
else: SEMANTIC_AVAILABLE = _installed("fastembed", "numpy")
BACKGROUND_DAEMONS_ALLOWED = (not settings.spawned_child and settings.write_origin == "foreground")

# ---- DB-path migration side-effect (preserve verbatim from old config.py) ----
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# ... the ~/.memory_partner -> ~/.threadkeeper one-shot copy block, using DB_PATH ...
```

- [ ] **Step 4: Enumerate exports.** Run `grep -rhoE "from \.\.?config import [^\n]+" threadkeeper/ | tr ',' '\n' | grep -oE "[A-Z_]+[A-Z]" | sort -u` and ensure every name is re-exported by the shim.
- [ ] **Step 5: Run config tests.** `.venv/bin/python -m pytest tests/test_config_settings.py -q` → PASS.
- [ ] **Step 6: Regression.** `.venv/bin/python -m pytest -q` → all pass (proves the 52 import sites still resolve). Read the summary line.
- [ ] **Step 7: Commit.** `git add threadkeeper/config.py tests/test_config_settings.py && git commit -m "refactor: config.py -> pydantic-settings Settings with compat shim"`

---

### Task 3: Refactor spawn_config.py to read `settings.spawn`

**Files:**
- Modify: `threadkeeper/spawn_config.py`
- Test: `tests/test_spawn_config.py` (extend if exists, else create)

- [ ] **Step 1: Write failing parity tests** — for each: per-role from `settings.spawn.loop`, `default`, active-CLI fallback, `"claude"` last; model from `settings.spawn.model[role]` then `[cli]` then `""`. Drive via monkeypatched env (`THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex`, `THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet`) + reload config.

```python
def test_resolve_agent_role_from_spawn(monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER", "codex")
    import threadkeeper.config as c, importlib; importlib.reload(c)
    import threadkeeper.spawn_config as sc; importlib.reload(sc)
    assert sc.resolve_agent("shadow_observer", active_cli="claude") == "codex"
    assert sc.resolve_agent("unknown_role", active_cli="claude") == "claude"  # active CLI

def test_resolve_model(monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN__MODEL__CLAUDE", "sonnet")
    import threadkeeper.config as c, importlib; importlib.reload(c)
    import threadkeeper.spawn_config as sc; importlib.reload(sc)
    assert sc.resolve_model("claude", role="shadow_observer") == "sonnet"
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Replace `_load_file`/`_config_path`/`tomllib`/`THREADKEEPER_SPAWN_CONFIG` and the file/env override helpers with reads from `config.settings.spawn`:

```python
from . import config
SUPPORTED_CLIS = ("claude", "codex", "gemini", "copilot")

def _sp(): return config.settings.spawn

def resolve_agent(role: str, active_cli: str | None = None) -> str:
    r = (role or "").lower()
    cli = _sp().loop.get(r)
    if cli and cli != "auto" and cli in SUPPORTED_CLIS: return cli
    d = _sp().default.lower()
    if d and d != "auto" and d in SUPPORTED_CLIS: return d
    if active_cli in SUPPORTED_CLIS: return active_cli
    return "claude"

def resolve_model(cli: str, role: str = "") -> str:
    m = _sp().model
    if role and m.get(role.lower()): return m[role.lower()]
    if m.get(cli.lower()): return m[cli.lower()]
    return ""
```
Update `summary_table` to read `_sp()` and drop the file/env source labels (label: "spawn config" / "active CLI" / "fallback"). Update the module docstring to describe the nested `.env` keys.

- [ ] **Step 4: Run spawn tests** → PASS.
- [ ] **Step 5: Regression** `.venv/bin/python -m pytest -q` → all pass.
- [ ] **Step 6: Commit.** `git add threadkeeper/spawn_config.py tests/test_spawn_config.py && git commit -m "refactor: spawn_config reads settings.spawn; retire spawn.toml reads"`

---

### Task 4: `.env.example` generator + `.gitignore`

**Files:**
- Create: `scripts/gen_env_example.py`
- Create/overwrite: `.env.example`
- Modify: `.gitignore`
- Test: `tests/test_env_example.py`

- [ ] **Step 1: Test** — assert every `Settings` field (prefixed) appears as a key in `.env.example`:

```python
def test_env_example_covers_all_fields():
    from threadkeeper.config import Settings
    text = open(".env.example").read()
    for name in Settings.model_fields:
        if name == "spawn": continue
        assert f"THREADKEEPER_{name.upper()}" in text or name in ("claude_skills_dir","claude_projects_dir")
```

- [ ] **Step 2: Implement generator** `scripts/gen_env_example.py` — iterate `Settings.model_fields`, emit `# <description>\nTHREADKEEPER_<NAME>=<default>` lines grouped by a `# --- <section> ---` comment; emit the spawn nested keys (`THREADKEEPER_SPAWN__DEFAULT=`, `THREADKEEPER_SPAWN__LOOP__<ROLE>=`, `THREADKEEPER_SPAWN__MODEL__<CLI>=`) as commented examples. Write `.env.example`.
- [ ] **Step 3: Generate** `.venv/bin/python scripts/gen_env_example.py`.
- [ ] **Step 4: .gitignore** — add a line `.env` (and confirm `~/.threadkeeper/` is already ignored / out of repo).
- [ ] **Step 5: Test** `.venv/bin/python -m pytest tests/test_env_example.py -q` → PASS.
- [ ] **Step 6: Commit.** `git add scripts/gen_env_example.py .env.example .gitignore tests/test_env_example.py && git commit -m "feat: generate .env.example from Settings schema; gitignore .env"`

---

### Task 5: Installer + spawn.toml retirement + user migration

**Files:**
- Modify: `threadkeeper/_setup.py`, `install.sh` (remove spawn.toml creation/refs; ensure `.env` seeded from `.env.example` if absent)
- Runtime (not committed): migrate the user's `~/.threadkeeper/spawn.toml` → `~/.threadkeeper/.env`, then remove spawn.toml.

- [ ] **Step 1:** `grep -rn "spawn.toml\|spawn_config\|SPAWN_CONFIG" threadkeeper/_setup.py install.sh` — find every reference.
- [ ] **Step 2:** In `_setup.py`/`install.sh`, replace any spawn.toml scaffolding with: if `~/.threadkeeper/.env` absent, copy `.env.example` there. Remove spawn.toml mentions.
- [ ] **Step 3: Migrate the live user file** (runtime, one-shot): append the user's current spawn.toml values to `~/.threadkeeper/.env` as nested keys (default=claude, the 7 loop roles=claude, model claude=sonnet — per spec), then `rm ~/.threadkeeper/spawn.toml`.
- [ ] **Step 4: Regression** `.venv/bin/python -m pytest -q` → all pass.
- [ ] **Step 5: Commit.** `git add threadkeeper/_setup.py install.sh && git commit -m "feat: installer seeds ~/.threadkeeper/.env; retire spawn.toml"`

---

### Task 6: Docs

**Files:** `README.md`, `docs/ARCHITECTURE.md`, `CONTRIBUTING.md`, `CHANGELOG.md`

- [ ] **Step 1:** Update each: config now a single `~/.threadkeeper/.env` (typed pydantic-settings), `.env.example` is the documented template, spawn.toml retired (breaking), nested `THREADKEEPER_SPAWN__*` keys. CHANGELOG under `[Unreleased]`: an `### Added` (single-.env config) + `### Changed`/breaking (spawn.toml retired, config → pydantic-settings).
- [ ] **Step 2: Commit.** `git add README.md docs/ARCHITECTURE.md CONTRIBUTING.md CHANGELOG.md && git commit -m "docs: single-.env config + spawn.toml retirement"`

---

### Task 7: Final regression + cleanup

- [ ] **Step 1:** Remove the spike file: `rm -f /tmp/tk_pyd_spike.py`.
- [ ] **Step 2: Full suite** `.venv/bin/python -m pytest -q > /tmp/tk_full.log 2>&1; echo "EXIT=$?"; grep -E "[0-9]+ passed" /tmp/tk_full.log | tail -1` — confirm EXIT=0 and read the `N passed` line.
- [ ] **Step 3:** Smoke: `THREADKEEPER_DB=/tmp/smoke.db .venv/bin/python -c "from threadkeeper.tools.threads import brief; print(brief()[:80])"` → renders without error.
- [ ] **Step 4:** If anything failed, fix + re-run before declaring done.

---

## Self-review

- **Spec coverage:** Settings+shim (Task 2) ✓, spawn fold+refactor (Task 3) ✓, .env.example+gitignore (Task 4) ✓, installer+migration (Task 5) ✓, dependency (Task 1) ✓, docs (Task 6) ✓, testing throughout ✓, process-scoped vars (brief_lean etc. are Settings fields, set via real env by hook/spawn — covered in Task 2 field list) ✓. Open items resolved by the spike (baked into Task 2/3).
- **Placeholders:** the "mirror remaining fields from old config.py" is a precise instruction against a concrete source file (not a TBD); tricky cases (aliases, derived, migration, spawn) have full code.
- **Type consistency:** `settings.spawn` (`SpawnSettings.default/loop/model`) referenced identically in Task 2 (definition) and Task 3 (consumption); shim names match the current `config import` usages (verified via the Step-4 grep in Task 2).
