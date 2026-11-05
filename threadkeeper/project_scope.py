"""Privacy-safe project namespaces and memory-scope helpers.

Project memory is isolated by default.  The identifier is a digest of a Git
remote when one is available, so it is stable across worktrees and directory
moves without placing a local path in the database or tool output.  A Git
history fingerprint is the safe fallback for repositories that have not yet
configured a remote.  Non-repository directories remain unresolved and their
memory is intentionally not recalled automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Literal


MemoryScope = Literal["project", "global", "shared", "unresolved"]
RECALLABLE_SCOPES = ("project", "global", "shared")
PORTABLE_SCOPES = ("global", "shared")
_VALID_SCOPES = frozenset((*RECALLABLE_SCOPES, "unresolved"))
_NAMESPACE_RE = re.compile(r"^prj_[a-f0-9]{20}$")


@dataclass(frozen=True)
class ProjectNamespace:
    """The current project identity, never containing a local path."""

    project_id: str | None
    source: str

    @property
    def resolved(self) -> bool:
        return self.project_id is not None


def _git(cwd: Path, *args: str) -> str:
    # Test/process launch shims may replace Popen with a minimal child stub.
    # Namespace resolution must never turn such a shim into an extra launch;
    # unresolved is the safe answer until normal subprocess support returns.
    if not hasattr(subprocess.Popen, "__enter__"):
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError, TypeError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _normalise_remote(value: str) -> str:
    """Remove credentials and cosmetic URL differences before fingerprinting."""
    remote = value.strip()
    if not remote:
        return ""
    # git@host:owner/repo and ssh://git@host/owner/repo identify the same repo.
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", remote)
    if scp:
        remote = f"ssh://{scp.group(1)}/{scp.group(2)}"
    remote = re.sub(r"://[^/@]+@", "://", remote)
    remote = remote.rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote.lower()


def _fingerprint(value: str) -> str:
    return "prj_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def resolve_project_namespace(cwd: str | Path | None = None) -> ProjectNamespace:
    """Resolve a stable namespace for ``cwd`` without exposing its path.

    ``THREADKEEPER_PROJECT_NAMESPACE`` is intentionally a digest-shaped
    operator override for unusual non-Git projects.  It does not accept an
    arbitrary repository name, preventing an accidental local-path leak.
    """
    override = os.environ.get("THREADKEEPER_PROJECT_NAMESPACE", "").strip()
    resolved_cwd = str(Path(cwd or os.getcwd()).resolve())
    return _resolve_project_namespace(resolved_cwd, override)


@lru_cache(maxsize=32)
def _resolve_project_namespace(cwd: str, override: str) -> ProjectNamespace:
    """Resolve one path/override pair once per process.

    Namespace identity is deliberately based on repository data rather than
    mutable working-tree files. Caching avoids repeatedly spawning Git while
    rendering a brief, which also keeps a single brief internally consistent.
    """
    if _NAMESPACE_RE.fullmatch(override):
        return ProjectNamespace(override, "override")
    root = _git(Path(cwd), "rev-parse", "--show-toplevel")
    if not root:
        return ProjectNamespace(None, "unresolved")
    repo = Path(root)
    remotes = _git(repo, "remote").splitlines()
    remote_name = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
    remote = _normalise_remote(_git(repo, "remote", "get-url", remote_name))
    if remote:
        return ProjectNamespace(_fingerprint(f"remote:{remote}"), "remote")
    # The root commit is stored in the repository object database, not its
    # location, so this survives ordinary path moves and Git worktrees.
    root_commit = _git(repo, "rev-list", "--max-parents=0", "--all").splitlines()
    history = root_commit[0] if root_commit else _git(repo, "rev-parse", "HEAD")
    if history:
        return ProjectNamespace(_fingerprint(f"history:{history}"), "git-history")
    return ProjectNamespace(None, "unresolved")


def current_project_id() -> str | None:
    return resolve_project_namespace().project_id


def namespace_for_transcript_label(label: str) -> ProjectNamespace:
    """Resolve transcript provenance only when its project label is exact.

    Transcript adapters often expose only a lossy display label.  Guessing
    would turn another repository's conversation into current-project memory,
    so unknown labels intentionally stay unresolved.  Claude Code's encoded
    directory label and simple basename labels are the safe cases.
    """
    cwd = Path(os.getcwd()).resolve()
    value = (label or "").strip()
    candidates = {
        cwd.name,
        str(cwd).replace("/", "-"),
        str(cwd).replace(os.sep, "-"),
    }
    if value not in candidates:
        return ProjectNamespace(None, "transcript-unresolved")
    return resolve_project_namespace(cwd)


def resolve_write_scope(scope: str = "project") -> tuple[MemoryScope, str | None]:
    """Validate an explicit scope and resolve its project namespace.

    A default project write outside a resolvable project becomes
    ``unresolved``.  This is a deliberate fail-closed state, never an
    implicit global-memory promotion.
    """
    requested = (scope or "project").strip().lower()
    if requested not in _VALID_SCOPES - {"unresolved"}:
        raise ValueError("memory_scope must be project, global, or shared")
    if requested in PORTABLE_SCOPES:
        return requested, None
    project_id = current_project_id()
    return ("project", project_id) if project_id else ("unresolved", None)


def recall_allowed(memory_scope: str | None, project_id: str | None,
                   *, current: str | None = None,
                   cross_project: bool = False) -> bool:
    """Return whether a scoped row is visible to the current recall request."""
    from .memory_access import current_access
    policy = current_access()
    if policy.child and policy.namespace != "*":
        if not policy.namespace and memory_scope not in PORTABLE_SCOPES:
            return False
        if memory_scope == "project" and project_id != policy.namespace:
            return False
    if memory_scope in PORTABLE_SCOPES:
        return True
    if memory_scope != "project":
        return False
    # Fresh database rows created by compatible low-level callers may omit the
    # additive namespace columns. They are current-project rows only; legacy
    # migrations are explicitly stamped ``unresolved`` and never take this
    # path.
    if not project_id:
        return bool(current if current is not None else current_project_id())
    if cross_project:
        return True
    return project_id == (current if current is not None else current_project_id())


def _recall_where(alias: str, *, current: str | None = None,
                 cross_project: bool = False) -> tuple[str, tuple[str, ...]]:
    """SQL predicate for schema-v5 scoped tables.

    Unresolved legacy records are excluded even in explicit cross-project
    recall. They need an explicit operator classification before becoming
    recallable.
    """
    if cross_project:
        return f"{alias}.memory_scope IN ('project','global','shared')", ()
    project_id = current if current is not None else current_project_id()
    if project_id:
        return (
            f"({alias}.memory_scope IN ('global','shared') OR "
            f"({alias}.memory_scope='project' AND "
            f"({alias}.project_id=? OR {alias}.project_id IS NULL)))",
            (project_id,),
        )
    return f"{alias}.memory_scope IN ('global','shared')", ()


def recall_where(alias: str, *, current: str | None = None,
                 cross_project: bool = False) -> tuple[str, tuple[str, ...]]:
    from .memory_access import current_access, scope_predicate
    normal, params = _recall_where(alias, current=current, cross_project=cross_project)
    policy = current_access()
    if not policy.child:
        return normal, params
    cap, cap_params = scope_predicate(alias, policy)
    return f"({normal}) AND ({cap})", (*params, *cap_params)


def format_provenance(memory_scope: str | None, project_id: str | None) -> str:
    """A visible, path-free provenance label for retrieval output."""
    if memory_scope == "project":
        return f"scope=project project={project_id or 'unresolved'}"
    return f"scope={memory_scope or 'unresolved'}"
