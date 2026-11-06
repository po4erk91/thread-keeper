"""Scheduled updater for installed Skill.md directories.

The updater has two jobs:

- keep mirrored CLI skill roots in sync by importing the newest local copy into
  the primary root and mirroring it everywhere else;
- update source-tracked GitHub skills when their upstream directory changed.

It deliberately updates SKILL.md directories, not arbitrary files. Skills with
local edits after the last source-tracked update are skipped instead of being
overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from urllib import request
from urllib.parse import quote
import zipfile

from . import identity
from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    CLAUDE_SKILLS_DIR,
    DB_PATH,
    SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE,
    SKILL_UPDATE_INFER_SOURCES,
    SKILL_UPDATE_INTERVAL_S,
    SKILL_UPDATE_SOURCES,
    SKILL_UPDATE_TIMEOUT_S,
)
from .db import get_db
from .helpers import daemon_sleep, single_flight_lock
from .skill_provenance import verify_publisher

logger = logging.getLogger(__name__)

SOURCE_FILE = ".threadkeeper-skill-source.json"
EVENT_KIND = "skill_update_pass"
_started = False


@dataclass(frozen=True)
class GithubSource:
    repo: str
    ref: str
    path: str


@dataclass(frozen=True)
class RemoteSkill:
    name: str
    source: GithubSource
    path: Path
    tree_sha256: str
    resolved_commit: str


@dataclass
class SkillCopy:
    root: Path
    path: Path
    name: str
    tree_sha256: str
    mtime: int
    primary: bool


def _record_skill_update_pass(summary: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, '', ?, ?)",
            (
                identity._session_id or "",
                EVENT_KIND,
                summary,
                int(time.time()),
            ),
        )
        conn.commit()
    except Exception:
        logger.debug("skill_updater: failed to record pass", exc_info=True)


def _last_skill_update_ts() -> int:
    try:
        row = get_db().execute(
            "SELECT created_at FROM events WHERE kind=? "
            "ORDER BY created_at DESC LIMIT 1",
            (EVENT_KIND,),
        ).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    try:
        return int(row["created_at"] or 0)
    except (TypeError, ValueError):
        return 0


def _due(now: int | None = None) -> tuple[bool, int]:
    if SKILL_UPDATE_INTERVAL_S <= 0:
        return False, 0
    now = int(now or time.time())
    last = _last_skill_update_ts()
    age = now - last if last else int(SKILL_UPDATE_INTERVAL_S)
    return last == 0 or age >= SKILL_UPDATE_INTERVAL_S, max(0, age)


def _update_lock():
    return single_flight_lock("skill-update")


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _skip_rel(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in {SOURCE_FILE, ".DS_Store", ".git", "__pycache__"}:
        return True
    return any(part in {".git", "__pycache__"} for part in parts)


def _tree_sha256(root: Path) -> str:
    """Hash every file except our local lock with unambiguous framing."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.as_posix() == SOURCE_FILE or not path.is_file():
            continue
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _legacy_tree_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _skip_rel(rel) or path.is_dir():
            continue
        if not path.is_file():
            continue
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


def _tree_mtime(root: Path) -> int:
    latest = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if _skip_rel(rel):
            continue
        try:
            latest = max(latest, int(path.stat().st_mtime))
        except OSError:
            continue
    return latest


def _skill_name(path: Path) -> str:
    md = path / "SKILL.md"
    try:
        body = md.read_text(encoding="utf-8")
    except OSError:
        return path.name
    try:
        from .tools.skills import _parse_frontmatter

        fields, err = _parse_frontmatter(body)
        if not err and fields and fields.get("name"):
            return str(fields["name"])
    except Exception:
        return path.name
    return path.name


def _skill_roots() -> list[Path]:
    from .tools.skills import _skill_roots as configured_skill_roots

    roots: list[Path] = []
    seen: set[Path] = set()
    for root in configured_skill_roots():
        try:
            key = root.expanduser().resolve()
        except OSError:
            key = root.expanduser()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root.expanduser())
    return roots


def _iter_skill_copies() -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    primary_root = CLAUDE_SKILLS_DIR.expanduser().resolve()
    for root in _skill_roots():
        if not root.exists():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if not _is_skill_dir(child):
                continue
            try:
                is_primary = child.parent.resolve() == primary_root
            except OSError:
                is_primary = False
            copies.append(
                SkillCopy(
                    root=root,
                    path=child,
                    name=_skill_name(child),
                    tree_sha256=_tree_sha256(child),
                    mtime=_tree_mtime(child),
                    primary=is_primary,
                )
            )
    return copies


def _validate_skill_dir(path: Path, expected_name: str | None = None) -> str | None:
    md = path / "SKILL.md"
    if not md.is_file():
        return "missing_SKILL.md"
    try:
        body = md.read_text(encoding="utf-8")
    except OSError as e:
        return f"read_failed: {e}"
    try:
        from .tools.skills import _validate_skill_md

        name = expected_name or _skill_name(path)
        return _validate_skill_md(body, name)
    except Exception as e:  # noqa: BLE001
        return f"validate_failed: {e}"


def _replace_skill_tree(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def _backup_skill_dir(skill_dir: Path) -> None:
    if not skill_dir.exists():
        return
    stamp = int(time.time())
    backup_root = DB_PATH.parent / "skill-update-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    dst = backup_root / f"{skill_dir.name}-{stamp}"
    idx = 1
    while dst.exists():
        dst = backup_root / f"{skill_dir.name}-{stamp}-{idx}"
        idx += 1
    shutil.copytree(skill_dir, dst, symlinks=True)


def _mirror_skill(name: str) -> None:
    from .tools.skills import _mirror_skill_dir

    _mirror_skill_dir(name)


def _removal_time(name: str) -> int:
    """When the primary copy of a skill was last deleted or archived, or 0."""
    try:
        row = get_db().execute(
            "SELECT MAX(created_at) AS at FROM events WHERE target=? AND ("
            "kind='skill_delete' OR "
            "(kind='curator_transition' AND summary LIKE '%→archived%'))",
            (name,),
        ).fetchone()
    except Exception:
        return 0
    return int(row["at"] or 0) if row else 0


def _drop_stale_strays(name: str, copies: list[SkillCopy]) -> bool:
    """Delete leftover copies of a deliberately removed skill.

    A deletion or archive removes the primary copy; an older copy left in
    another CLI root must not be imported back as the newest version. A copy
    written after the removal is a new install and is left alone.
    """
    removed_at = _removal_time(name)
    if not removed_at or any(copy.mtime > removed_at for copy in copies):
        return False
    for copy in copies:
        if copy.path.is_symlink():
            continue
        _backup_skill_dir(copy.path)
        shutil.rmtree(copy.path, ignore_errors=True)
    return True


def _sync_local_roots() -> dict[str, int]:
    counts = {
        "local_updates": 0,
        "mirrored": 0,
        "conflicts": 0,
        "local_checked": 0,
        "removed_strays": 0,
    }
    grouped: dict[str, list[SkillCopy]] = {}
    for copy in _iter_skill_copies():
        grouped.setdefault(copy.name, []).append(copy)

    primary_root = CLAUDE_SKILLS_DIR.expanduser()
    for name, copies in grouped.items():
        counts["local_checked"] += 1
        primary = next((c for c in copies if c.primary), None)
        if primary is None and _drop_stale_strays(name, copies):
            counts["removed_strays"] += 1
            continue
        # Tracked copies move only through the upstream verification gate.
        if any((copy.path / SOURCE_FILE).exists() for copy in copies):
            continue
        newest_mtime = max(c.mtime for c in copies)
        newest = [c for c in copies if c.mtime == newest_mtime]
        newest_hashes = {c.tree_sha256 for c in newest}
        if len(newest_hashes) != 1:
            counts["conflicts"] += 1
            continue

        distinct_hashes = {c.tree_sha256 for c in copies}
        if primary and len(distinct_hashes) <= 1:
            continue

        winner = newest[0]
        if primary and primary.tree_sha256 == winner.tree_sha256:
            if err := _validate_skill_dir(primary.path, name):
                logger.debug("skill_updater: not mirroring %s: %s", name, err)
                counts["conflicts"] += 1
                continue
            _mirror_skill(name)
            counts["mirrored"] += 1
            continue

        if primary and primary.path.is_symlink():
            counts["conflicts"] += 1
            continue

        dst = primary_root / name
        if err := _validate_skill_dir(winner.path, name):
            logger.debug("skill_updater: skip local %s: %s", name, err)
            counts["conflicts"] += 1
            continue
        _backup_skill_dir(dst)
        _replace_skill_tree(winner.path, dst)
        _mirror_skill(name)
        counts["local_updates"] += 1
    return counts


def _parse_sources(raw: str) -> list[GithubSource]:
    specs: list[GithubSource] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            logger.warning("Ignoring invalid skill update source: %s", item)
            continue
        left, path = item.split(":", 1)
        if "@" in left:
            repo, ref = left.rsplit("@", 1)
        else:
            repo, ref = left, "main"
        repo = repo.strip().strip("/")
        ref = (ref or "main").strip()
        path = path.strip().strip("/")
        if not _valid_source(repo, path):
            logger.warning("Ignoring invalid skill update source: %s", item)
            continue
        specs.append(GithubSource(repo=repo, ref=ref, path=path))
    return specs


def _valid_source(repo: str, path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                and all(part not in {"", ".", ".."} for part in repo.split("/"))
                and path and not Path(path).is_absolute()
                and all(part not in {"", ".", ".."} for part in path.split("/")))


def _source_record(skill_dir: Path) -> dict:
    try:
        data = json.loads((skill_dir / SOURCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_source_file(skill_dir: Path) -> GithubSource | None:
    data = _source_record(skill_dir)
    if data.get("type") != "github":
        return None
    repo = str(data.get("repo") or "").strip().strip("/")
    ref = str(data.get("ref") or "main").strip()
    src_path = str(data.get("path") or "").strip().strip("/")
    if not _valid_source(repo, src_path):
        return None
    return GithubSource(repo=repo, ref=ref, path=src_path)


def _write_source_file(
    skill_dir: Path, remote: RemoteSkill, *, inferred: bool,
    record: dict, verification: str,
) -> None:
    data = {
        "lock_version": 2,
        "type": "github",
        "repo": remote.source.repo,
        "ref": remote.source.ref,
        "path": remote.source.path,
        "tree_sha256": remote.tree_sha256,
        "resolved_commit": remote.resolved_commit,
        "verification_policy": record["verification_policy"],
        "expected_signer": record.get("expected_signer"),
        "last_verification": {"result": verification, "checked_at": int(time.time())},
        "source_url": (
            f"https://github.com/{remote.source.repo}/tree/"
            f"{remote.resolved_commit}/{remote.source.path}"
        ),
        "inferred": bool(inferred),
        "updated_at": int(time.time()),
    }
    (skill_dir / SOURCE_FILE).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    for info in zip_file.infolist():
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError("archive_symlink")
        extracted = (dest_dir / info.filename).resolve()
        if extracted == dest_root or dest_root in extracted.parents:
            continue
        raise ValueError("archive contains files outside destination")
    zip_file.extractall(dest_dir)


def _resolve_commit(source: GithubSource) -> str:
    url = f"https://api.github.com/repos/{source.repo}/commits/{quote(source.ref, safe='')}"
    req = request.Request(url, headers={"User-Agent": "threadkeeper-skill-updater",
                                        "Accept": "application/vnd.github+json"})
    with request.urlopen(req, timeout=SKILL_UPDATE_TIMEOUT_S) as res:
        commit = json.load(res).get("sha", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit_mismatch")
    if re.fullmatch(r"[0-9a-fA-F]{40}", source.ref) and source.ref.lower() != commit:
        raise ValueError("commit_mismatch")
    return commit


def _download_repo_zip(source: GithubSource, dest_dir: Path, commit: str) -> Path:
    url = f"https://codeload.github.com/{source.repo}/zip/{commit}"
    req = request.Request(url, headers={"User-Agent": "threadkeeper-skill-updater"})
    zip_path = dest_dir / "repo.zip"
    with request.urlopen(req, timeout=SKILL_UPDATE_TIMEOUT_S) as res:
        zip_path.write_bytes(res.read())
    with zipfile.ZipFile(zip_path, "r") as zf:
        top_levels = {name.split("/")[0] for name in zf.namelist() if name}
        if top_levels != {f"{source.repo.split('/')[-1]}-{commit}"}:
            raise ValueError("commit_mismatch")
        _safe_extract_zip(zf, dest_dir)
    return dest_dir / next(iter(top_levels))


def _remote_skills_from_source(
    source: GithubSource, repo_root: Path, commit: str,
) -> list[RemoteSkill]:
    if not _valid_source(source.repo, source.path):
        raise ValueError("source_invalid")
    base = repo_root / source.path
    candidates: list[Path]
    if _is_skill_dir(base):
        candidates = [base]
    elif base.is_dir():
        candidates = [
            child for child in base.iterdir()
            if child.is_dir() and not child.name.startswith(".") and _is_skill_dir(child)
        ]
    else:
        return []

    out: list[RemoteSkill] = []
    for path in candidates:
        name = _skill_name(path)
        src_path = path.relative_to(repo_root).as_posix()
        exact_source = GithubSource(
            repo=source.repo,
            ref=source.ref,
            path=src_path,
        )
        out.append(
            RemoteSkill(
                name=name,
                source=exact_source,
                path=path,
                tree_sha256=_tree_sha256(path),
                resolved_commit=commit,
            )
        )
    return out


def _build_remote_index(
    sources: list[GithubSource],
    tmp_root: Path,
) -> tuple[dict[tuple[str, str, str], RemoteSkill], dict[str, RemoteSkill], list[str]]:
    by_exact: dict[tuple[str, str, str], RemoteSkill] = {}
    by_name: dict[str, RemoteSkill] = {}
    ambiguous: set[str] = set()
    errors: list[str] = []
    repo_cache: dict[tuple[str, str], tuple[Path, str]] = {}

    for source in sources:
        try:
            if not _valid_source(source.repo, source.path):
                raise ValueError("source_invalid")
            key = (source.repo, source.ref)
            if key not in repo_cache:
                repo_tmp = tmp_root / str(len(repo_cache))
                repo_tmp.mkdir(parents=True, exist_ok=True)
                commit = _resolve_commit(source)
                repo_cache[key] = (_download_repo_zip(source, repo_tmp, commit), commit)
            repo_root, commit = repo_cache[key]
            for remote in _remote_skills_from_source(source, repo_root, commit):
                exact = (remote.source.repo, remote.source.ref, remote.source.path)
                by_exact[exact] = remote
                if remote.name in by_name and by_name[remote.name].source != remote.source:
                    ambiguous.add(remote.name)
                else:
                    by_name[remote.name] = remote
        except Exception as exc:  # Network errors can contain paths or credentials.
            reason = str(exc) if isinstance(exc, ValueError) else ""
            errors.append(reason if reason in {"commit_mismatch", "source_invalid", "archive_symlink"}
                          else "source_fetch_failed")

    for name in ambiguous:
        by_name.pop(name, None)
    return by_exact, by_name, errors


def _source_specs_for_primary_skills() -> list[GithubSource]:
    specs = _parse_sources(SKILL_UPDATE_SOURCES)
    seen = {(s.repo, s.ref, s.path) for s in specs}
    for skill_dir in _primary_skill_dirs():
        source = _read_source_file(skill_dir)
        if not source:
            continue
        key = (source.repo, source.ref, source.path)
        if key in seen:
            continue
        seen.add(key)
        specs.append(source)
    return specs


def _primary_skill_dirs() -> list[Path]:
    root = CLAUDE_SKILLS_DIR.expanduser()
    if not root.exists():
        return []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    return [
        child for child in children
        if child.is_dir() and not child.name.startswith(".") and _is_skill_dir(child)
    ]


def _apply_remote_update(
    skill_dir: Path,
    remote: RemoteSkill,
    *,
    inferred: bool,
) -> tuple[str, str]:
    name = _skill_name(skill_dir)
    if remote.name != name or _validate_skill_dir(remote.path, name):
        return "error_remote_invalid", "remote_invalid"
    record = _source_record(skill_dir)
    local_hash = _tree_sha256(skill_dir)
    expected_hash = record.get("tree_sha256")
    guard_hash = local_hash if record.get("lock_version") == 2 else _legacy_tree_sha256(skill_dir)
    if expected_hash and expected_hash != guard_hash:
        return "skipped_local_changes", "not_checked"
    verification = _verify_remote(remote, record)
    if verification not in {"verified", "unsigned_allowed"}:
        return "error_verification", verification
    if _mirror_has_local_changes(skill_dir, record, guard_hash):
        return "skipped_local_changes", verification
    if inferred and local_hash != remote.tree_sha256 and not SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE:
        return "skipped_untracked_changed", verification
    if local_hash == remote.tree_sha256:
        _write_source_file(skill_dir, remote, inferred=inferred,
                           record=record, verification=verification)
        _mirror_skill(name)
        return "no_update", verification

    _backup_skill_dir(skill_dir)
    _replace_skill_tree(remote.path, skill_dir)
    _write_source_file(skill_dir, remote, inferred=inferred,
                       record=record, verification=verification)
    _mirror_skill(remote.name)
    return "updated", verification


def _mirror_has_local_changes(skill_dir: Path, record: dict, baseline: str) -> bool:
    hash_tree = _tree_sha256 if record.get("lock_version") == 2 else _legacy_tree_sha256
    for root in _skill_roots():
        mirror = root / skill_dir.name
        if mirror.resolve() == skill_dir.resolve() or not _is_skill_dir(mirror):
            continue
        if hash_tree(mirror) != baseline:
            return True
    return False


def _verify_remote(remote: RemoteSkill, record: dict) -> str:
    if record.get("lock_version", 2) != 2:
        return "lock_version_unsupported"
    commit = remote.resolved_commit
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "commit_mismatch"
    if re.fullmatch(r"[0-9a-fA-F]{40}", remote.source.ref) and remote.source.ref.lower() != commit:
        return "commit_mismatch"
    if (remote.path / SOURCE_FILE).exists():
        return "upstream_lock_refused"
    if any(path.is_symlink() for path in remote.path.rglob("*")):
        return "remote_symlink"
    if _tree_sha256(remote.path) != remote.tree_sha256:
        return "digest_mismatch"
    if (record.get("lock_version") == 2 and record.get("resolved_commit") == commit
            and record.get("tree_sha256") != remote.tree_sha256):
        return "digest_mismatch"
    return verify_publisher(remote.path, record)


def _update_source_tracked_skills() -> dict:
    counts: dict = {
        "remote_checked": 0,
        "remote_updated": 0,
        "remote_no_update": 0,
        "remote_skipped": 0,
        "remote_errors": [],
        "verification": {},
    }
    sources = _source_specs_for_primary_skills()
    if not sources:
        return counts
    with tempfile.TemporaryDirectory(prefix="threadkeeper-skill-update-") as td:
        by_exact, by_name, errors = _build_remote_index(sources, Path(td))
        counts["remote_errors"] = errors
        for error in errors:
            outcomes = counts["verification"]
            outcomes[error] = outcomes.get(error, 0) + 1
        for skill_dir in _primary_skill_dirs():
            source = _read_source_file(skill_dir)
            remote: RemoteSkill | None = None
            inferred = False
            if source:
                remote = by_exact.get((source.repo, source.ref, source.path))
            elif SKILL_UPDATE_INFER_SOURCES:
                remote = by_name.get(_skill_name(skill_dir))
                inferred = remote is not None
            if not remote:
                continue
            counts["remote_checked"] = int(counts["remote_checked"]) + 1
            result, verification = _apply_remote_update(skill_dir, remote, inferred=inferred)
            outcomes = counts["verification"]
            outcomes[verification] = outcomes.get(verification, 0) + 1
            if result == "updated":
                counts["remote_updated"] = int(counts["remote_updated"]) + 1
            elif result == "no_update":
                counts["remote_no_update"] = int(counts["remote_no_update"]) + 1
            elif result.startswith("error_"):
                errors.append(result)
            else:
                counts["remote_skipped"] = int(counts["remote_skipped"]) + 1
    return counts


def _run_update() -> str:
    local = _sync_local_roots()
    remote = _update_source_tracked_skills()
    remote_errors = remote.get("remote_errors", [])
    error_count = len(remote_errors) if isinstance(remote_errors, list) else 0
    verification = ",".join(f"{result}:{count}" for result, count
                            in sorted(remote["verification"].items())) or "none"
    return (
        f"verification={verification} "
        f"local_updates={local['local_updates']} mirrored={local['mirrored']} "
        f"conflicts={local['conflicts']} local_checked={local['local_checked']} "
        f"removed_strays={local['removed_strays']} "
        f"remote_updated={remote['remote_updated']} "
        f"remote_no_update={remote['remote_no_update']} "
        f"remote_skipped={remote['remote_skipped']} "
        f"remote_checked={remote['remote_checked']} errors={error_count}"
    )


def run_skill_update_pass(*, force: bool = False) -> str:
    """Run one skill update check/apply pass.

    `force=True` bypasses the interval gate but still respects the process lock.
    """
    if SKILL_UPDATE_INTERVAL_S <= 0 and not force:
        return "disabled"
    if not force:
        is_due, age = _due()
        if not is_due:
            return f"not_due age_s={age}"

    with _update_lock() as locked:
        if not locked:
            return "update_running"
        try:
            result = _run_update()
        except Exception as e:  # noqa: BLE001
            logger.debug("skill_updater: pass failed", exc_info=True)
            result = f"error {type(e).__name__}"

    _record_skill_update_pass(result)
    return result


def _serve_loop() -> None:
    while True:
        try:
            run_skill_update_pass()
        except Exception:
            logger.debug("skill_updater daemon tick failed", exc_info=True)
        daemon_sleep(SKILL_UPDATE_INTERVAL_S, idle_s=3600.0)


def start_skill_update_daemon() -> None:
    """Start the twice-weekly skill updater in foreground MCP parents."""
    global _started
    from .daemon_liveness import daemon_thread_alive, start_daemon_thread
    if _started and daemon_thread_alive("skill_update_daemon"):
        return
    if SKILL_UPDATE_INTERVAL_S <= 0:
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    start_daemon_thread("skill_update_daemon", _serve_loop)
    _started = True
