# Contributing to thread-keeper

Quick map of the project so new patches land cleanly.

## Project layout

```
threadkeeper/            # the package
├── server.py            # MCP entry: python -m threadkeeper.server
├── _mcp.py              # FastMCP singleton (shared @mcp.tool registry)
├── _setup.py            # `thread-keeper-setup` installer
├── config.py            # env-driven defaults
├── db.py                # SQLite schema + sqlite-vec loader
├── identity.py          # session + self-cid + daemon launchers
├── embeddings.py        # optional sentence-transformers
├── ingest.py            # adapter-driven transcript ingest
├── brief.py             # render_brief / render_context
├── shadow_review.py     # autonomous learning observer
├── i18n.py              # multilingual regex / prompt bundles
├── adapters/            # one file per CLI
│   ├── base.py          # CLIAdapter ABC + NormalizedMessage
│   ├── claude_code.py
│   ├── codex.py
│   ├── gemini.py
│   ├── copilot.py
│   └── _hook_helpers.py
└── tools/               # @mcp.tool entries
    ├── threads.py       # brief, note, open_thread, close_thread, ...
    ├── peers.py         # broadcast, whisper, inbox, wait, ...
    ├── spawn.py         # spawn primitive + tasks
    ├── skills.py        # skill_manage, skill_record, review_thread
    ├── dialectic.py     # user-model claims / evidence
    └── ...
```

## Running tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[semantic,dev]'
python -m pytest
```

Test isolation uses a tempdir DB per test, all daemons disabled via env
(`THREADKEEPER_*_INTERVAL_S=0`). See `tests/conftest.py`.

## Adding a new CLI adapter

1. Create `threadkeeper/adapters/<name>.py` exporting `ADAPTER`
   (instance of `CLIAdapter` from `base.py`).
2. Implement the abstract methods:
   - `is_installed()` — detect the CLI on disk
   - `register_mcp_server(...)` / `unregister_mcp_server(...)` — wire
     thread-keeper into the CLI's MCP config (TOML / JSON / sqlite —
     each adapter knows its native format)
   - `transcript_files()` / `iter_messages(fp)` — yield
     `NormalizedMessage` objects from the CLI's conversation history
3. Optional hooks:
   - `instructions_path()` — point at the CLI's per-user instructions
     file (CLAUDE.md / AGENTS.md / GEMINI.md / etc.). Setup writes the
     managed thread-keeper block there.
   - `hooks_supported()` + `register_hooks()` — install SessionStart /
     PostToolUse hooks if the CLI has a hook framework.
4. Append the adapter to `ADAPTERS` in `adapters/__init__.py`.
5. Add tests in `tests/test_adapters.py` covering:
   - MCP register/unregister round-trip
   - `iter_messages` against a synthetic transcript fixture
   - (if applicable) hook installation

Setup, ingest, and brief picks up the new adapter automatically.

## Adding a new language to the i18n bundle

All locale strings live in `threadkeeper/i18n.py` — the rest of the
codebase imports named constants from there and stays English-only.
To add a language:

1. In `i18n.py`, append your patterns to:
   - `_PARALLEL_WORDS_BOUNDED` or `_PARALLEL_WORDS_CJK`
     (latter is the no-`\b` branch — pick CJK if your script has no
     whitespace word boundary, otherwise stick with bounded)
   - `_COUNT_WORDS` (numerals + words)
   - `_PLURAL_NOUNS` (tasks/questions/etc. heads)
   - `_WANT_<LANG>`, `_INSIGHT_<LANG>`, `_EXAMPLE_<LANG>`, `_FRAME_<LANG>`
2. Add the locale code to `SUPPORTED_LOCALES`.
3. Optionally extend `SHADOW_CLASS_SIGNAL_EXAMPLES` and
   `SPAWN_TRIGGER_PHRASE_EXAMPLES` with sample bilingual lines so the
   shadow-review LLM evaluator recognizes class-level signals in the
   new language.
4. Add a row per family to the parametrized samples in
   `tests/test_i18n_multilang.py`.

Currently shipped: en, zh, hi, es, pt, fr, de, ar, ru, ja
(~82% of the world's speakers).

## Style

- Code is English-only outside `i18n.py` (CI doesn't enforce this yet
  but a `grep -P '[\\x{0400}-\\x{04FF}]'` over `threadkeeper/` should
  return nothing except `i18n.py`).
- Lean docstrings — first paragraph explains *why*, second explains
  *how*. Skip the obvious.
- No emoji in code or docs.
- Tests are parametrized where natural; one parametrize per family
  beats five near-identical tests.

## Verification before PR

```bash
python -m pytest                                  # 358+ tests should pass
python scripts/tk_verify_ingest.py                # cross-CLI ingest sanity
python -m threadkeeper._setup --dry-run           # setup is idempotent
```

If any of these report something unexpected on a clean checkout, that's
a bug — please open an issue or a PR.
