# Releasing thread-keeper

Publishing to PyPI is deliberately split into two steps. A push to `main`
runs `.github/workflows/test.yml`; when that run succeeds,
`.github/workflows/release-tag.yml` checks whether the version in
`pyproject.toml` has matching changelog notes, but it does **not** create a tag,
dispatch publishing, or hold write permissions. A maintainer then authorizes the
release by pushing a signed annotated `v*` tag. `.github/workflows/publish.yml`
verifies that signed tag, checks that it matches `pyproject.toml` and
`CHANGELOG.md`, then pauses at the protected `pypi` GitHub Environment before
uploading.

PyPI upload uses Trusted Publisher OIDC — no PyPI API token is stored in the
repository. The publish job explicitly uploads PyPI digital attestations
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

### 2. Protected GitHub environment

In the repo: **Settings → Environments → New environment** → name
`pypi`.

Configure it as a required approval gate:

- Add at least one **Required reviewer**.
- Disable admin bypass if the repository policy requires two-person release
  control.
- Leave deployment branches/tags unrestricted, or allow the `v*` release tags
  used by `publish.yml`.

Verify the environment before relying on the release workflow:

```bash
gh api repos/po4erk91/thread-keeper/environments/pypi \
  --jq '.protection_rules'
```

The output must include a `required_reviewers` rule. Without that repository
setting, the signed tag is still required by source control, but the PyPI upload
job will not pause for a second human approval.

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
# 1. Bump version in pyproject.toml on a normal PR branch
$EDITOR pyproject.toml         # version = "0.4.0" → "0.4.1"

# 2. Add a matching CHANGELOG section: ## v0.4.1 — YYYY-MM-DD
$EDITOR CHANGELOG.md

# 3. Commit, open a PR, and merge it to main after tests pass
git commit -am "release: 0.4.1"
```

The workflows then:
1. Run the full test matrix on `main`
2. Check that an unreleased `vX.Y.Z` has a matching changelog section
3. Stop and print the signed-tag command for the maintainer

After the merge-to-main checks are green, publish with a signed annotated tag:

```bash
git fetch origin main --tags
git tag -s v0.4.1 origin/main -m "release: v0.4.1"
git push origin refs/tags/v0.4.1
```

The publish workflow then:

1. Verifies the workflow is running from a `v*` tag ref
2. Verifies the tag is annotated and GitHub reports its signature as valid
3. Confirms the tag matches `pyproject.toml` and `CHANGELOG.md`
4. Builds sdist + wheel (`python -m build`)
5. Runs `twine check dist/*`
6. Waits for the protected `pypi` environment approval
7. Uploads artifacts to PyPI via `pypa/gh-action-pypi-publish` (OIDC,
   no token)
8. Creates a GitHub Release from the matching `CHANGELOG.md` section

Watch progress at
<https://github.com/po4erk91/thread-keeper/actions>.

### Manual re-run

If a signed tag already exists and a publish run failed after authorization,
re-run the workflow against the tag ref:

```bash
gh workflow run publish.yml --ref v0.4.1
```

The re-run still verifies the signed tag and still waits at the `pypi`
environment.

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
# Bump patch + commit; then publish with the signed-tag flow above
$EDITOR pyproject.toml         # 0.4.1 → 0.4.2
git commit -am "release: 0.4.2 (yank 0.4.1)"
```

Then yank 0.4.1 via PyPI UI:
<https://pypi.org/manage/project/threadkeeper/release/0.4.1/> →
"Options" → "Yank release". Yanked versions stay installable for
people who pin them but are hidden from `pip install threadkeeper`.

## Pre-releases

Tag with PEP 440 suffix to publish to PyPI as a pre-release that
`pip install threadkeeper` skips by default:

```bash
git tag -s v0.5.0a1 -m "release: v0.5.0a1"      # alpha
git tag -s v0.5.0b1 -m "release: v0.5.0b1"      # beta
git tag -s v0.5.0rc1 -m "release: v0.5.0rc1"    # release candidate
```

Users opt in via `pip install --pre threadkeeper`.
