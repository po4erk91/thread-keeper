"""Task 2 tests: pydantic-settings Settings class in config.py.

Uses importlib.reload so each test can set/clear env before re-importing.
"""
import importlib
import os
import tempfile

import pytest


def _fresh_config(monkeypatch, env=None, env_file=None):
    """Reload threadkeeper.config with a clean env slate."""
    for k in list(os.environ):
        if k.startswith("THREADKEEPER_") or k in (
            "CLAUDE_SKILLS_DIR",
            "CLAUDE_PROJECTS_DIR",
        ):
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
    c2 = _fresh_config(
        monkeypatch,
        env={"THREADKEEPER_MEMORY_NUDGE_INTERVAL": "99"},
        env_file=path,
    )
    assert c2.MEMORY_NUDGE_INTERVAL == 99  # real env beats .env


def test_claude_dir_bare_alias(monkeypatch):
    c = _fresh_config(monkeypatch, env={"CLAUDE_SKILLS_DIR": "/tmp/x"})
    assert str(c.CLAUDE_SKILLS_DIR) == "/tmp/x"


def test_bad_type_raises(monkeypatch):
    with pytest.raises(Exception):
        _fresh_config(
            monkeypatch, env={"THREADKEEPER_MEMORY_NUDGE_INTERVAL": "nope"}
        )


def test_all_exported_names_present(monkeypatch):
    """Every name that the package imports from .config must exist as a module attr."""
    c = _fresh_config(monkeypatch)
    required = [
        "AUTO_REVIEW_ENABLED",
        "BACKGROUND_DAEMONS_ALLOWED",
        "BRIEF_LEAN",
        "BRIEF_NO_THREAD_NUDGE",
        "CANDIDATE_REVIEW_INTERVAL_S",
        "CANDIDATE_REVIEW_MIN",
        "CLIENT_LABEL",
        "CLAUDE_PROJECTS_DIR",
        "CLAUDE_SKILLS_DIR",
        "CURATOR_DESTRUCTIVE",
        "CURATOR_INTERVAL_S",
        "CURATOR_MIN_LESSONS",
        "CURATOR_REPORTS_DIR",
        "DB_PATH",
        "DIALECTIC_MAX_NEW_CLAIMS",
        "DIALECTIC_MINE_INTERVAL_S",
        "DIALECTIC_VALIDATE_BATCH_SIZE",
        "DIALECTIC_VALIDATE_INTERVAL_S",
        "DIALECTIC_VALIDATE_MIN",
        "DIALOG_LOG",
        "EMBED_BACKEND",
        "EMBED_MODEL_NAME",
        "EVOLVE_REVIEW_INTERVAL_S",
        "EVOLVE_REVIEW_MIN",
        "EXTRACT_INTERVAL_S",
        "EXTRACT_WINDOW_MIN",
        "FASTEMBED_MODEL_ID",
        "INGEST_CAP_PER_CALL",
        "INGEST_INTERVAL_S",
        "INGEST_RECENT_WINDOW_S",
        "MEMORY_GUARD_AGG_KILL_MB",
        "MEMORY_GUARD_AGG_WARN_MB",
        "MEMORY_GUARD_COOLDOWN_S",
        "MEMORY_GUARD_KILL_MB",
        "MEMORY_GUARD_NOTIFY",
        "MEMORY_GUARD_POLL_S",
        "MEMORY_GUARD_RECLAIM_MB",
        "MEMORY_GUARD_RETIRE_IDLE_S",
        "MEMORY_GUARD_RETIRE_LIVE",
        "MEMORY_GUARD_TARGET_SERVERS",
        "MEMORY_GUARD_WARN_MB",
        "MENUBAR_AUTO_LAUNCH",
        "MEMORY_NUDGE_INTERVAL",
        "NO_EMBEDDINGS",
        "PANEL_EFFORT",
        "PANEL_MODEL",
        "PANEL_REQUIRE_SKEPTIC",
        "PANEL_ROLES",
        "PANEL_SIZE",
        "PANEL_VOTE_WEIGHT",
        "PROBE_COOLDOWN_S",
        "PROBE_INTERVAL_S",
        "SELF_CID_TTL_S",
        "SEMANTIC_AVAILABLE",
        "SHADOW_REVIEW_INTERVAL_S",
        "SHADOW_REVIEW_MIN_CHARS",
        "SHADOW_REVIEW_WINDOW_S",
        "SKILL_NUDGE_INTERVAL",
        "SPAWN_BUDGET_MB",
        "SPAWN_BUDGET_POLL_S",
        "SPAWN_ESTIMATE_FULL_MB",
        "SPAWN_ESTIMATE_SLIM_MB",
        "SPAWNED_CHILD",
        "TASK_LOG_DIR",
        "THREAD_IDLE_CLOSE_DAYS",
        "THREAD_JANITOR_INTERVAL_S",
        "WRITE_ORIGIN",
    ]
    for name in required:
        assert hasattr(c, name), f"config.{name} is missing"


def test_db_path_type(monkeypatch):
    """DB_PATH must be a pathlib.Path, not a string."""
    from pathlib import Path
    c = _fresh_config(monkeypatch)
    assert isinstance(c.DB_PATH, Path)


def test_panel_roles_is_list(monkeypatch):
    """PANEL_ROLES must be a list of strings."""
    c = _fresh_config(monkeypatch)
    assert isinstance(c.PANEL_ROLES, list)
    assert "skeptic" in c.PANEL_ROLES


def test_spawn_settings_defaults(monkeypatch):
    """settings.spawn has the right defaults."""
    c = _fresh_config(monkeypatch)
    assert c.settings.spawn.default == ""
    assert c.settings.spawn.loop == {}
    assert c.settings.spawn.model == {}


def test_spawn_nested_env(monkeypatch):
    """THREADKEEPER_SPAWN__MODEL__CLAUDE populates spawn.model."""
    c = _fresh_config(
        monkeypatch, env={"THREADKEEPER_SPAWN__MODEL__CLAUDE": "sonnet"}
    )
    assert c.settings.spawn.model.get("claude") == "sonnet"
