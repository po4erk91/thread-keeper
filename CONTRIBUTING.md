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
│   ├── claude_desktop.py
│   ├── codex.py
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

Releases are fully automated — you don't bump the version, write the
changelog, or push a tag by hand. The flow:

1. PR title (Conventional Commits format, enforced by `pr-title.yml`)
   becomes the squash-merge commit message on `main`.
2. `.github/workflows/tests.yml` runs the full pytest matrix on the
   merge commit.
3. On green tests, `.github/workflows/release.yml` runs
   `python-semantic-release`, which:
   - reads commits since the last `v*` tag,
   - picks the highest bump implied by the types,
   - writes the new version into `pyproject.toml`,
   - appends a `CHANGELOG.md` entry,
   - commits the bump back to `main`,
   - creates an annotated tag `vX.Y.Z` and a GitHub release with the
     auto-generated notes.
4. The tag push triggers `publish.yml`, which builds sdist + wheel
   and uploads to PyPI via the Trusted Publisher OIDC flow. (Within
   release.yml we call `gh workflow run publish.yml --ref <tag>`
   explicitly because default `GITHUB_TOKEN` pushes don't trigger
   downstream workflows.)

### Required setup: RELEASE_PAT secret

Classic branch protection on `main` requires `contents:write` from an
account that bypasses the rules. `enforce_admins=false` means the
maintainer admin account does — but `github-actions[bot]` does not.
release.yml therefore needs a **fine-grained PAT** belonging to the
admin to push the version-bump commit and tag.

**One-time setup** (maintainer only):

1. GitHub → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → **Generate new token**.
2. Resource owner: your account. Repository access: only
   `po4erk91/thread-keeper`.
3. Permissions:
   - **Repository → Contents** → Read and write
   - **Repository → Metadata** → Read-only (auto-required)
4. Expiration: 1 year (or whatever your security policy mandates).
5. Copy the token.
6. Repo → Settings → Secrets and variables → Actions → **New
   repository secret**, name `RELEASE_PAT`, paste the token.

release.yml falls back to the default `GITHUB_TOKEN` when the secret
isn't set, but that path will fail loudly on push to main —
intentional so a missing token is surfaced, not silently swallowed.

### Bump policy

| Commit type                | Bump   | Example: 0.5.3 → |
|----------------------------|--------|------------------|
| `feat:`                    | minor  | 0.6.0            |
| `fix:`, `perf:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`, `build:`, `deps:`, `revert:` | patch  | 0.5.4 |
| `BREAKING CHANGE:` footer  | minor while in 0.x (`major_on_zero = false`); flip to `true` and re-release when promoting to 1.0.0 | 0.6.0 |

Commits whose type isn't in the table above (or whose summary line
fails the pr-title check) won't produce a release — they're treated as
no-ops by the parser. This is how we avoid "wip:" / typos / merge-
commit noise creating empty version bumps.

### Forcing or skipping a release

- **Force a release run**: Actions tab → "release" workflow →
  "Run workflow" on the `main` branch. Useful if a release got skipped
  (e.g. tests were re-run after a flake and the auto-trigger missed).
- **Skip a release for a specific commit**: prefix the type with a
  non-allowlisted token (or just don't merge until you have a real
  user-visible change to bundle with it). There's no `[skip release]`
  trailer — keep main releasable.

### Promoting to 1.0.0

When the API is stable and you want the next BREAKING CHANGE to bump
to 1.0.0:

1. Flip `[tool.semantic_release].major_on_zero = true` in
   `pyproject.toml` (under a `chore:` commit).
2. Land a commit with a `BREAKING CHANGE:` footer (or `feat!:` prefix).
3. Next release run produces `v1.0.0` automatically.
