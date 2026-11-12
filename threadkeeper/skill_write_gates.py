"""Write-time quality gates for skills, shared by every writer.

Learning-loop writers (background, candidate and shadow review) get hard
refusals. A session agent (foreground) gets the same checks; a few of them
are warnings so the human stays in charge, and ``force`` lets a foreground
writer keep a deliberate exception. The same function runs when a proposal is
queued and again when it is accepted, so an accepted proposal cannot skip a
gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import time

from . import skill_quality as quality

PATCH_REASONS = frozenset({"used_failed", "used_corrected", "contradicted"})
PATCH_USE_WINDOW_S = 14 * 86400
MIN_EVIDENCE_CHARS = 20
# A proposing child is recorded as 'spawned' (only trusted launches keep
# their claimed origin), so 'spawned' gets the strict loop rules too.
LEARNING_LOOP_ORIGINS = frozenset({
    "background_review", "candidate_review", "shadow_review", "spawned",
})
# Writers that author new skill knowledge. Maintenance writers (curator,
# evolve) repair and merge existing skills under their own verification, so
# only the wikilink rule applies to them.
GATED_ORIGINS = LEARNING_LOOP_ORIGINS | {"foreground"}


@dataclass(frozen=True)
class SkillWrite:
    """One requested skill write, described for the gates."""

    action: str
    name: str
    added: str
    full: str = ""
    description: str = ""
    project: str = ""
    evidence_sessions: str = ""
    patch_reason: str = ""
    evidence: str = ""
    new_despite: str = ""
    force: bool = False
    origin: str = "foreground"

    @property
    def loop(self) -> bool:
        return self.origin in LEARNING_LOOP_ORIGINS


@dataclass
class GateResult:
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def refuse_or_warn(self, write: SkillWrite, message: str, *, overridable: bool) -> None:
        if write.loop or not (overridable and write.force):
            self.error = self.error or message
            return
        self.warnings.append(message)


def _split_frontmatter(content: str) -> tuple[str, str]:
    """(description, body) of a full SKILL.md, or ('', content)."""
    from .skill_audit import _frontmatter

    fields, body, _error = _frontmatter(content or "")
    return str(fields.get("description") or ""), body


def _check_wikilinks(write: SkillWrite, result: GateResult) -> None:
    links = quality.wikilinks(write.added)
    if links:
        result.error = result.error or (
            f"ERR skill_wikilink links={','.join(links[:5])}; other CLIs cannot "
            "resolve [[...]] links, so write the rule text into the skill instead"
        )


def _check_history(write: SkillWrite, result: GateResult) -> None:
    noise = quality.history_noise(write.added)
    if not noise:
        return
    message = (
        f"ERR skill_history_noise found={','.join(noise[:6])}; generalize the "
        "episode into a rule: drop thread/ticket/PR IDs, dates, incident counts, "
        "version stories, user quotes and personal paths"
    )
    if write.loop:
        result.error = result.error or message
    else:
        result.warnings.append(message.replace("ERR ", "warn ", 1))


def _check_template(write: SkillWrite, result: GateResult) -> None:
    description, body = _split_frontmatter(write.full)
    gaps = quality.template_gaps(description or write.description, body)
    if gaps:
        result.refuse_or_warn(write, (
            f"ERR skill_template gaps={','.join(gaps)}; a new skill has a "
            "description that starts with 'Use when', and sections '## Not for', "
            "'## Steps', '## Check' and a one-line '## Why'"
        ), overridable=True)
    split = quality.split_candidate(body)
    if split:
        message = (
            f"ERR skill_split_candidate {split[0]}; keep one task class per "
            "skill and move detail to references/<topic>.md"
        )
        if write.loop:
            result.error = result.error or message
        else:
            result.warnings.append(message.replace("ERR ", "warn ", 1))


def _declared_project(content: str) -> str:
    from .skill_audit import _frontmatter

    metadata = _frontmatter(content or "")[0].get("metadata")
    return str(metadata.get("threadkeeper-project") or "") if isinstance(metadata, dict) else ""


def _check_project(write: SkillWrite, terms: tuple[str, ...], result: GateResult) -> None:
    description = (_split_frontmatter(write.full)[0] or write.description).lower()
    project = (write.project or _declared_project(write.full)).strip().lower()
    if project:
        if project not in description:
            result.error = result.error or (
                f"ERR skill_project_not_in_description project={project}; name "
                "the project in the description so the skill only routes there"
            )
        return
    scoped = set(quality.project_mentions(description, terms))
    mentioned = [term for term in quality.project_mentions(write.added, terms) if term not in scoped]
    if mentioned:
        result.refuse_or_warn(write, (
            f"ERR skill_project_examples projects={','.join(mentioned)}; a shared "
            "skill contains no project examples — generalize them, or create a "
            "project skill with project=<name>"
        ), overridable=True)


def _check_duplicates(conn: sqlite3.Connection, write: SkillWrite, threshold: float,
                      result: GateResult) -> None:
    from .skill_catalog import load_catalog, semantic_neighbors

    description = _split_frontmatter(write.full)[0] or write.description
    neighbors = semantic_neighbors(load_catalog(conn), write.name, description, limit=1)
    if not neighbors:
        return
    neighbor, score = neighbors[0]
    if score < threshold or write.new_despite.strip() == neighbor:
        return
    result.error = result.error or (
        f"ERR likely_duplicate_skill neighbor={neighbor} score={score:.2f}; "
        "patch it only when the task class and trigger are the same, otherwise "
        f"add references/<topic>.md there, or pass new_despite={neighbor} when "
        "the task class differs"
    )


def _sessions_and_days(conn: sqlite3.Connection, refs: list[str]) -> tuple[set[str], set[str]]:
    sessions: set[str] = set()
    days: set[str] = set()
    for ref in refs:
        rows = conn.execute(
            "SELECT session_id, MIN(created_at) AS first_at FROM dialog_messages "
            "WHERE session_id >= ? AND session_id < ? GROUP BY session_id LIMIT 2",
            (ref, ref + "￿"),
        ).fetchall()
        if len(rows) != 1:
            continue
        sessions.add(rows[0]["session_id"])
        days.add(time.strftime("%Y-%m-%d", time.gmtime(int(rows[0]["first_at"] or 0))))
    return sessions, days


def _check_repetition(conn: sqlite3.Connection, write: SkillWrite, minimum: int,
                      result: GateResult) -> None:
    refs = [ref.strip() for ref in write.evidence_sessions.split(",") if len(ref.strip()) >= 6]
    sessions, days = _sessions_and_days(conn, refs)
    if max(len(sessions), len(days)) >= minimum:
        return
    message = (
        f"ERR skill_needs_repetition sessions={len(sessions)} days={len(days)} "
        f"min={minimum}; pass evidence_sessions=<id,id> from distinct sessions "
        "where the same task came up. A single episode becomes a lesson, or "
        "waits in the candidate queue for a repeat"
    )
    if write.loop:
        result.error = result.error or message
    else:
        result.warnings.append(message.replace("ERR ", "warn ", 1))


def active_local_skills(conn: sqlite3.Connection, root: Path) -> list[str]:
    """Active production skills installed in the primary root.

    Plugin skills and names only seen in telemetry do not count: the cap is
    about the descriptions thread-keeper itself adds to every session.
    """
    rows = conn.execute(
        "SELECT name FROM skill_usage WHERE state='active' "
        "AND classification='production' AND availability='available'"
    ).fetchall()
    return [row["name"] for row in rows if (root / row["name"] / "SKILL.md").is_file()]


def _check_cap(conn: sqlite3.Connection, write: SkillWrite, cap: int, root: Path,
               result: GateResult) -> None:
    if cap <= 0:
        return
    active = len(active_local_skills(conn, root))
    if active < cap:
        return
    message = (
        f"ERR skill_active_cap active={active} cap={cap}; every description "
        "costs context — extend or retire an existing skill first"
    )
    if write.loop:
        result.error = result.error or message
    else:
        result.warnings.append(message.replace("ERR ", "warn ", 1))


def _last_use(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(last_used_at, 0) AS used FROM skill_usage WHERE name=?", (name,),
    ).fetchone()
    return int(row["used"] if row else 0)


def patches_since_last_use(conn: sqlite3.Connection, name: str) -> int:
    """Skill writes recorded after the skill's most recent use."""
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE target=? AND created_at>? AND kind IN "
        "('skill_patch','skill_edit','skill_write_file')",
        (name, _last_use(conn, name)),
    ).fetchone()
    return int(row[0] if row else 0)


def _check_patch_evidence(conn: sqlite3.Connection, write: SkillWrite, result: GateResult) -> None:
    if not write.loop or write.action not in {"edit", "patch"}:
        return
    reason = write.patch_reason.strip()
    if reason not in PATCH_REASONS:
        result.error = result.error or (
            "ERR skill_patch_reason_required (used_failed|used_corrected|"
            "contradicted); patch a skill only when it was used and failed or "
            "was corrected, or new facts contradict it"
        )
        return
    if reason.startswith("used_") and _last_use(conn, write.name) < time.time() - PATCH_USE_WINDOW_S:
        result.error = result.error or (
            f"ERR skill_patch_without_use reason={reason}; the skill has no "
            "recorded use in the last 14 days, so it cannot have failed in use"
        )
        return
    if len(write.evidence.strip()) < MIN_EVIDENCE_CHARS:
        result.error = result.error or (
            "ERR skill_patch_evidence_required; state in one line what failed, "
            "what the user corrected, or which new fact contradicts the skill"
        )


def _check_patch_cap(conn: sqlite3.Connection, write: SkillWrite, cap: int,
                     result: GateResult) -> None:
    if cap <= 0:
        return
    count = patches_since_last_use(conn, write.name)
    if count < cap:
        return
    message = (
        f"ERR skill_patch_cap_without_use patches={count} cap={cap}; nobody "
        "has used this skill since its last patches — wait for real use"
    )
    if write.loop:
        result.error = result.error or message
    else:
        result.warnings.append(message.replace("ERR ", "warn ", 1))


@dataclass(frozen=True)
class GateSettings:
    duplicate_threshold: float
    project_terms: tuple[str, ...]
    min_sessions: int
    patch_cap: int
    active_cap: int
    skills_root: Path


def check_skill_write(conn: sqlite3.Connection, write: SkillWrite,
                      settings: GateSettings, *, history_checks: bool = True) -> GateResult:
    """Run every gate that applies to ``write``; the first error wins.

    ``history_checks=False`` skips the gates that need the full memory store
    (repetition, recorded use, caps). A proposing child cannot see that store;
    those gates run again when the proposal is accepted.
    """
    result = GateResult()
    _check_wikilinks(write, result)
    if write.origin not in GATED_ORIGINS:
        return result
    _check_history(write, result)
    _check_project(write, settings.project_terms, result)
    if write.action == "create":
        _check_template(write, result)
        _check_duplicates(conn, write, settings.duplicate_threshold, result)
        if history_checks:
            _check_repetition(conn, write, settings.min_sessions, result)
            _check_cap(conn, write, settings.active_cap, settings.skills_root, result)
        return result
    if history_checks:
        _check_patch_evidence(conn, write, result)
        _check_patch_cap(conn, write, settings.patch_cap, result)
    if write.action in {"edit", "patch"}:
        split = quality.split_candidate(_split_frontmatter(write.full)[1])
        if split:
            result.warnings.append(
                f"warn skill_split_candidate {split[0]}; this skill now holds "
                "more than one topic — flag it for a split"
            )
    return result
