# Contributing to thread-keeper

Quick map of the project so new patches land cleanly.

- By participating you agree to follow the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- Found a security-sensitive issue? Please report it via the
  process in [SECURITY.md](SECURITY.md), not a public issue.
- For questions and design discussion use
  [Discussions](https://github.com/po4erk91/thread-keeper/discussions);
  bug reports and feature proposals go through the issue templates.

## Project layout

```
threadkeeper/            # the package
├── server.py            # MCP entry: python -m threadkeeper.server
├── _mcp.py              # FastMCP singleton (shared @mcp.tool registry)
├── _setup.py            # `thread-keeper-setup` installer
├── config.py            # pydantic-settings Settings ← ~/.threadkeeper/.env
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
│   ├── claude_desktop.py
│   ├── codex.py
│   ├── antigravity.py
│   ├── gemini.py
│   ├── copilot.py
│   ├── vscode.py
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
python -m pytest                 # full suite
python -m pytest -m "not slow"   # fast inner loop — skips the embedding-warmup tests
```

CI runs the full suite under `--forked` (each test in its own process; the
per-test package re-import otherwise piles up native ONNX/tokenizer thread
pools that can deadlock sqlite finalize in one long-lived interpreter).

Test isolation uses a tempdir DB per test, all daemons disabled via env
(`THREADKEEPER_*_INTERVAL_S=0`). See `tests/conftest.py`.

`test_onnx_embeddings.py` carries `pytestmark = pytest.mark.slow` (it warms up
the embedding model); `-m "not slow"` skips it. The evolve tests were once the
suite's dominant cost — each ran a real `git clone` + venv + `pip install` — but
their bootstraps (and the shared `fresh_mp` fixture) now pin a ready tmp
checkout, so they are fast and run in every mode.

`pytest-xdist` ships in the `dev` extra as an **opt-in local** accelerator
(`pytest -n auto`). It is not the CI default: the suite is not yet fully
parallel-safe (a few tests race on shared state) and `-n auto` does not compose
with `--forked`. See [#217](https://github.com/po4erk91/thread-keeper/issues/217).
On macOS, combining `-n` with `--forked` also needs
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`.

## Schema migrations

SQLite schema changes live in `threadkeeper/db.py` and must be versioned with
`CURRENT_SCHEMA_VERSION`.

1. Add new baseline tables/indexes to `SCHEMA` for fresh installs.
2. Add upgrade DDL/data backfills to the version-gated migration path, then
   bump `CURRENT_SCHEMA_VERSION`.
3. Keep migrations idempotent by intent. For legacy `ALTER TABLE ... ADD
   COLUMN`, only duplicate-column errors may be ignored; any other
   `sqlite3.OperationalError` should be logged and raised so a partial schema
   does not masquerade as healthy.
4. Add tests for v0 upgrade, version stamping, and any concurrency or data
   backfill behavior the migration relies on.

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
python -m pytest                                  # 495+ tests should pass
python scripts/tk_verify_ingest.py                # cross-CLI ingest sanity
python -m threadkeeper._setup --dry-run           # setup is idempotent
```

If any of these report something unexpected on a clean checkout, that's
a bug — please open an issue or a PR.

## Pull request workflow

### Scope

One PR = one logical change. Don't bundle a new adapter with a
prompt-tuning fix with a README rewrite — split them. Reviewers will
ask you to anyway, and split-after is more work than split-before.

Rough size guidance:

- **Small** (1 file, <50 lines) — fix a typo, add a missing classifier,
  bump a constant. No discussion needed, ship it.
- **Medium** (2–5 files, <300 lines) — new MCP tool, new env knob, new
  test, prompt change. Standard PR; description + checklist + tests.
- **Large** (>300 lines or >5 files) — new adapter, new daemon, new
  schema migration, refactor across modules. Open as **Draft** first
  with a short design note in the PR description, get feedback on the
  shape before grinding through the implementation.

### Commit messages

We use Conventional Commits Lite — `<type>: <imperative summary>`:

| Type       | When to use                                                    |
|------------|----------------------------------------------------------------|
| `feat`     | New user-visible capability (MCP tool, adapter, daemon, knob)  |
| `fix`      | Behavior bug fix                                               |
| `refactor` | Internal restructure with no user-visible change               |
| `test`     | Test-only addition or rework                                   |
| `docs`     | README / CONTRIBUTING / inline-docstring changes               |
| `chore`    | Repo hygiene (templates, .gitignore, scaffolding)              |
| `ci`       | CI / release pipeline                                          |
| `deps`     | Dependency bump                                                |

Summary line is imperative and ≤72 chars. Body is optional and explains
*why* + non-obvious consequences. Example:

```
feat: extract daemon — auto-harvest decision-shaped utterances

Closes the agent-doesn't-call-note() gap by harvesting from
dialog_messages instead. Same scaffolding as shadow_review + curator
(cursor in events.kind='extract_pass', SEMANTIC_AVAILABLE guard).
```

### Branch names

`<type>/<short-slug>` where type matches the commit prefix. Examples:

- `feat/add-jetbrains-adapter`
- `fix/shadow-review-cursor-off-by-one`
- `docs/clarify-curator-destructive-mode`
- `ci/dependabot-grouping`

Forks: same convention on your fork.

### Linking issues

If the PR closes an issue, put `Closes #N` (or `Fixes #N`) in the
PR description — GitHub auto-closes the issue on merge.

For partial work that doesn't close yet: `Refs #N` so the issue tracks
the PR in its sidebar without auto-closing.

### Draft vs ready

Open as **Draft** when:

- you want early design feedback
- CI is still red and you're iterating
- the PR depends on another PR landing first

Switch to **Ready for review** when CI is green and the diff is what
you want the reviewer to see. Re-running CI after a forced-push counts
as a fresh review surface — re-request review if you got one already.

### Merge strategy

Branch protection requires linear history — no merge commits.
Maintainer will choose between:

- **Squash and merge** (default) — collapses the PR into one commit on
  main, rewriting the message to a clean Conventional Commits form.
  Use when the per-commit history is just "wip / fix lint / address
  review feedback" — noise the main branch doesn't need.
- **Rebase and merge** — keeps each commit as-is. Use only when every
  commit is independently meaningful, well-messaged, and the PR is
  small enough to read commit-by-commit. Rare.

Contributors don't need to pre-squash — the maintainer does it at
merge time. Just keep your branch rebased on `main` so the merge stays
fast-forward-able (branch protection has `strict=true`).

### After merge

- Delete your branch (GitHub offers a button) — keeps the fork tidy.
- If you got merged and want credit on the changelog: thanks, you're
  already on it. The Releases page picks up commit authors.

### Stale PRs

A PR with no activity for 30 days and red CI gets a polite ping from
the maintainer asking if it's still alive. After 60 days idle it
gets closed with `stale` label and a link to re-open if the
contributor returns.

## Releases

Versions follow Conventional-Commits-driven semver, executed by the
maintainer as part of each commit to `main`. The flow:

1. PR title (Conventional Commits format, enforced by `pr-title.yml`)
   becomes the squash-merge commit message on `main`.
2. **Same commit**: maintainer bumps `version` in `pyproject.toml`
   per the bump policy below and adds a `CHANGELOG.md` entry.
3. **Same push**: maintainer creates an annotated tag `vX.Y.Z` on the
   bump commit and pushes both the commit and the tag.
4. `.github/workflows/tests.yml` runs the full pytest matrix on push.
5. The tag push triggers `.github/workflows/publish.yml`, which builds
   sdist + wheel and uploads to PyPI via the Trusted Publisher OIDC
   flow.

There's no CI-side release automation — solo-repo, all commits go
through the maintainer anyway, and the `python-semantic-release`
machinery hit branch-protection friction (default `GITHUB_TOKEN` can't
push to a protected `main`; the alternatives — PAT secret, GitHub App,
release-please-style PR — all add per-release friction or one-time
setup that wasn't worth it at this scale). Pulled it out; the rule
lives in this section instead.

### Bump policy

| Commit type | Bump | Example: 0.5.3 → |
|---|---|---|
| `feat:` | minor | 0.6.0 |
| `fix:`, `perf:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`, `build:`, `deps:`, `revert:` | patch | 0.5.4 |
| `BREAKING CHANGE:` footer | minor while in 0.x (manual promotion to 1.0.0 when API is stable) | 0.6.0 |

If a single commit touches multiple concerns (rare — squash-merge
typically prevents this), pick the highest bump that applies.

### Tagging recipe

```bash
# Update pyproject.toml `version` per the bump table; bump server.json
# `version` AND `packages[0].version` to the same value (MCP Registry
# ownership verification matches PyPI version); add a CHANGELOG.md entry
# under a new `## vX.Y.Z — YYYY-MM-DD` heading.

git add pyproject.toml server.json CHANGELOG.md <other files for this change>
git commit -m "feat: <imperative summary>"
git tag -a vX.Y.Z -m "release vX.Y.Z"
git push && git push --tags
```

The tag push fans out to `publish.yml` automatically. After PyPI
upload completes, republish the MCP Registry entry so the new version
shows up there too:

```bash
mcp-publisher login github
mcp-publisher publish
```

### Promoting to 1.0.0

Once the API is stable and the next BREAKING CHANGE should land 1.0.0:
bump `pyproject.toml` to `1.0.0` directly, tag `v1.0.0`, push. No
config flip needed — manual control by design.
