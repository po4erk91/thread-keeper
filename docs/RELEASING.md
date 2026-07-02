# Releasing thread-keeper

Publishing to PyPI is automated via GitHub Actions. A push to `main`
runs `.github/workflows/test.yml`; when that run succeeds,
`.github/workflows/release-tag.yml` creates an annotated `v*` tag from
the version in `pyproject.toml` and dispatches
`.github/workflows/publish.yml` on that tag ref. Manual annotated tags
matching `v*` still trigger `publish.yml` directly.

PyPI upload uses Trusted Publisher OIDC — no PyPI API token is stored in
the repository. The publish job explicitly uploads PyPI digital attestations
(`attestations: true`), which is what the packaged auto-updater verifies before
running future `pip install --upgrade` self-updates.

## One-time setup

You need to do this **once**, before the first release.

### 1. PyPI trusted publisher

1. Sign in at <https://pypi.org/manage/account/publishing/>.
2. Click **Add a new pending publisher**.
3. Fill in:
   - **PyPI Project Name**: `threadkeeper`
   - **Owner**: `po4erk91`
   - **Repository name**: `thread-keeper`
   - **Workflow filename**: `publish.yml`
   - **Environment name**: `pypi`
4. Save. The first successful publish from GitHub Actions will create
   the project on PyPI and convert the pending publisher to active.

### 2. GitHub environment (optional but recommended)

In the repo: **Settings → Environments → New environment** → name
`pypi`. You can optionally add required reviewers / wait timers if you
want manual approval before each publish.

If you skip this step the workflow still works — GitHub creates the
environment on the fly when the job first runs.

### 3. Verify locally before tagging (optional)

```bash
pip install build twine
python -m build
twine check dist/*
```

Both `dist/threadkeeper-X.Y.Z-py3-none-any.whl` and
`dist/threadkeeper-X.Y.Z.tar.gz` should report `PASSED`. Don't `twine
upload` manually — let the workflow do it so the publish trail stays
on GitHub.

## Per-release flow

```bash
# 1. Bump version in pyproject.toml
$EDITOR pyproject.toml         # version = "0.4.0" → "0.4.1"

# 2. Add a matching CHANGELOG section: ## v0.4.1 — YYYY-MM-DD
$EDITOR CHANGELOG.md

# 3. Commit + push to main
git commit -am "release: 0.4.1"
git push origin main
```

The workflows then:
1. Run the full test matrix on `main`
2. Create annotated tag `vX.Y.Z` if it does not already exist
3. Dispatch `publish.yml` on that tag ref
4. Build sdist + wheel (`python -m build`)
5. Run `twine check dist/*`
6. Upload artifacts to PyPI via `pypa/gh-action-pypi-publish` (OIDC,
   no token)
7. Create a GitHub Release from the matching `CHANGELOG.md` section

Watch progress at
<https://github.com/po4erk91/thread-keeper/actions>.

### Manual tag fallback

If the post-test tag workflow is disabled or needs to be bypassed, you
can still publish by creating the annotated tag yourself:

```bash
git tag -a v0.4.1 -m "release: v0.4.1"
git push origin v0.4.1
```

Manual tag pushes trigger `publish.yml` directly.

## Versioning

Semantic versioning, conservative bumps:

- **PATCH** (`0.4.0` → `0.4.1`) — bugfixes, doc tweaks, internal
  refactors, prompt-tuning, calibration. No new MCP tools, no removed
  ones, no breaking config changes.
- **MINOR** (`0.4.0` → `0.5.0`) — new MCP tools, new env knobs, new
  daemons, new adapters, new prompt variants. Backwards compatible.
- **MAJOR** (`0.4.0` → `1.0.0`) — breaking changes to MCP tool
  signatures, removed tools, schema migrations that require manual
  intervention, removed env knobs. Bump only when truly necessary.

## Yank / re-release

If a release ships broken, **don't try to overwrite**. PyPI rejects
re-uploads of an existing version. Instead:

```bash
# Bump patch + commit; the post-test release workflow tags it
$EDITOR pyproject.toml         # 0.4.1 → 0.4.2
git commit -am "release: 0.4.2 (yank 0.4.1)"
git push origin main
```

Then yank 0.4.1 via PyPI UI:
<https://pypi.org/manage/project/threadkeeper/release/0.4.1/> →
"Options" → "Yank release". Yanked versions stay installable for
people who pin them but are hidden from `pip install threadkeeper`.

## Pre-releases

Tag with PEP 440 suffix to publish to PyPI as a pre-release that
`pip install threadkeeper` skips by default:

```bash
git tag v0.5.0a1            # alpha
git tag v0.5.0b1            # beta
git tag v0.5.0rc1           # release candidate
```

Users opt in via `pip install --pre threadkeeper`.
