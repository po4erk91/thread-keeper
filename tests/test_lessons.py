"""CLI-agnostic lessons store at ~/.threadkeeper/lessons.md.

Backs the materialization side of the learning loop for clients that
still need a CLI-agnostic fallback (Copilot and bare MCP) —
see issue #7.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import re
import sys
import time
from pathlib import Path

import pytest


def _lessons_rmw_worker(
    lessons_path: str,
    action: str,
    title: str,
    errors: mp.Queue,
) -> None:
    os.environ["THREADKEEPER_LESSONS"] = lessons_path
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    original_read_text = Path.read_text
    target = Path(lessons_path)

    def slow_lesson_read(path: Path, *args, **kwargs):
        body = original_read_text(path, *args, **kwargs)
        if path == target:
            time.sleep(0.1)
        return body

    Path.read_text = slow_lesson_read
    try:
        from threadkeeper import lessons

        if action == "append":
            lessons.append_lesson(
                title=title,
                body=f"{title} body",
                summary=f"{title} summary",
                source="shadow",
            )
        elif action == "remove":
            lessons.remove_lesson(title)
        else:  # pragma: no cover - test helper guard.
            raise AssertionError(f"unknown action: {action}")
    except BaseException as e:  # pragma: no cover - surfaced in parent.
        errors.put(repr(e))
        raise


def _bootstrap(tmp_path, monkeypatch, write_origin="foreground"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_WRITE_ORIGIN": write_origin,
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, lessons
    from threadkeeper._mcp import mcp
    return {
        "db": db,
        "lessons": lessons,
        "path": Path(env["THREADKEEPER_LESSONS"]),
        "mcp": mcp,
    }


# ─────────────────────────────────────────────────────────────────────
# Core API
# ─────────────────────────────────────────────────────────────────────

def test_append_creates_file_with_header(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert not pkg["path"].exists()
    slug = pkg["lessons"].append_lesson(
        title="Always paginate", body="Use offset+limit, never slice locally.",
        summary="DB queries — paginate at the source.", source="Tabc",
    )
    assert slug == "always-paginate"
    body = pkg["path"].read_text()
    assert "# thread-keeper lessons" in body
    assert "## always-paginate" in body
    assert "Use offset+limit" in body
    assert "source=Tabc" in body
    assert "origin=foreground" in body


def test_slugify_handles_messy_titles(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    slug = pkg["lessons"].append_lesson(
        title="Don't trim TRAILING whitespace!!! (it's load-bearing)",
        body="some body",
    )
    # All non-alnum collapsed to single hyphens, lowercased
    assert re.fullmatch(r"[a-z0-9-]+", slug)
    assert "trailing" in slug
    assert "whitespace" in slug


def test_append_with_same_slug_replaces_in_place(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(title="x", body="original body")
    pkg["lessons"].append_lesson(title="x", body="updated body")
    body = pkg["path"].read_text()
    assert body.count("<!-- LESSON:BEGIN slug=x ") == 1
    assert "original body" not in body
    assert "updated body" in body


def test_iter_lessons_returns_in_file_order(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(title="first", body="b1", source="T1")
    pkg["lessons"].append_lesson(title="second", body="b2", source="T2")
    pkg["lessons"].append_lesson(title="third", body="b3", source="shadow")
    items = list(pkg["lessons"].iter_lessons())
    assert [i["slug"] for i in items] == ["first", "second", "third"]
    assert items[2]["source"] == "shadow"


def test_concurrent_append_remove_preserves_distinct_edits(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(
        title="keep me", body="still here", source="shadow",
    )
    pkg["lessons"].append_lesson(
        title="remove me", body="delete me", source="shadow",
    )

    ctx = mp.get_context("spawn")
    errors = ctx.Queue()
    jobs = [
        ctx.Process(
            target=_lessons_rmw_worker,
            args=(str(pkg["path"]), "remove", "remove-me", errors),
        )
    ]
    titles = [f"concurrent lesson {i}" for i in range(8)]
    jobs.extend(
        ctx.Process(
            target=_lessons_rmw_worker,
            args=(str(pkg["path"]), "append", title, errors),
        )
        for title in titles
    )

    for job in jobs:
        job.start()
    for job in jobs:
        job.join(timeout=10)
        if job.is_alive():
            job.terminate()
            job.join(timeout=2)
            pytest.fail("lesson writer process did not exit")

    assert [job.exitcode for job in jobs] == [0] * len(jobs)
    assert errors.empty()
    body = pkg["path"].read_text()
    assert "slug=keep-me" in body
    assert "slug=remove-me" not in body
    for title in titles:
        assert f"slug={title.replace(' ', '-')}" in body


def test_count_zero_when_no_file(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["lessons"].count_lessons() == 0


# ─────────────────────────────────────────────────────────────────────
# MCP tools
# ─────────────────────────────────────────────────────────────────────

def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def test_mcp_lesson_append_validates_inputs(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    assert la(title="", body="x").startswith("ERR empty_title")
    assert la(title="x", body="").startswith("ERR empty_body")
    out = la(title="ok one", body="content")
    assert out.startswith("ok slug=ok-one")


def test_lesson_neighbors_previews_ranked_slugs_without_writing(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ln = _tool(pkg, "lesson_neighbors")
    la(title="reset ios network", body="Reset iOS simulator network settings.")
    la(title="serialize device boot", body="Serialize device boot locks.")
    la(title="unrelated release notes", body="Document release notes.")

    import threadkeeper.embeddings as embeddings

    def fake_vector(text: str) -> list[float]:
        if "recover simulator" in text:
            return [1.0, 0.0]
        if "Reset iOS simulator" in text:
            return [0.96, 0.0]
        if "Serialize device" in text:
            return [0.83, 0.0]
        return [0.40, 0.0]

    monkeypatch.setattr(
        embeddings,
        "encode_many",
        lambda texts: [fake_vector(text) for text in texts],
    )
    import threadkeeper.tools.lessons as lesson_tools

    lesson_tools.clear_lesson_vector_cache()

    out = ln(
        title="recover simulator connectivity",
        summary="Restore device networking before a test run.",
        body="Reset simulator network settings before retrying device tests.",
        k=2,
    )

    assert out.splitlines() == [
        "lesson_neighbors total=2 mode=semantic prospective_slug=recover-simulator-connectivity",
        "  1. reset-ios-network score=0.96",
        "  2. serialize-device-boot score=0.83",
    ]
    assert pkg["lessons"].count_lessons() == 3


def test_lesson_neighbors_leaves_existing_lesson_write_semantics_unchanged(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    ln = _tool(pkg, "lesson_neighbors")
    la = _tool(pkg, "lesson_append")

    assert ln(title="new lesson", body="new body") == (
        "lesson_neighbors total=0 mode=semantic"
    )
    assert la(title="new lesson", body="new body") == (
        f"ok slug=new-lesson path={pkg['path']}"
    )


def test_lesson_neighbors_falls_back_to_lexical_ranking(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ln = _tool(pkg, "lesson_neighbors")
    la(
        title="reset simulator network",
        body="Reset simulator network settings before device tests.",
    )
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)
    out = ln(
        title="repair simulator connectivity",
        body="Reset simulator network before retrying device tests.",
    )
    assert "mode=lexical" in out
    assert "reset-simulator-network" in out


def test_mcp_lesson_patch_replaces_one_unique_substring(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lp = _tool(pkg, "lesson_patch")
    la(
        title="retry strategy",
        summary="Retry only transient failures.",
        body=(
            "Use equal jitter for transient errors; do not retry errors "
            "indefinitely."
        ),
        source="shadow",
    )

    out = lp(
        slug="retry-strategy",
        old_string="equal jitter",
        new_string="decorrelated jitter",
    )

    assert out.startswith("ok slug=retry-strategy")
    section = pkg["path"].read_text()
    assert "decorrelated jitter" in section
    assert "equal jitter" not in section
    assert "source=shadow" in section
    assert "Retry only transient failures." in section
    assert lp(
        slug="retry-strategy", old_string="missing", new_string="x",
    ) == "ERR old_string_not_found"
    assert lp(
        slug="retry-strategy", old_string="errors", new_string="x",
    ).startswith("ERR old_string_ambiguous")


def test_shadow_lesson_append_rejects_overlong_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    body = "word " * 451
    out = la(title="compact rules only", body=body, source="shadow")
    assert out.startswith("ERR shadow_lesson_too_long")
    assert "max=450" in out


def test_shadow_lesson_append_allows_non_growing_same_slug_repair(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    original = "word " * 451
    repaired = "rule " + "word " * 450
    pkg["lessons"].append_lesson(
        title="long shadow lesson", body=original, source="shadow",
    )

    out = la(title="long shadow lesson", body=repaired, source="shadow")

    assert out.startswith("ok slug=long-shadow-lesson")
    assert "rule" in pkg["path"].read_text()
    assert la(
        title="long shadow lesson",
        body="longer " + "word " * 450,
        source="shadow",
    ).startswith("ERR shadow_lesson_too_long")


def test_shadow_lesson_append_rejects_near_duplicate_slug(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)

    first = la(
        title="better auth jwks poisoning recovery",
        body="Delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    assert first.startswith("ok")
    second = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="Diagnose and delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    assert second.startswith("ERR likely_duplicate_lesson")
    assert "better-auth-jwks-poisoning-recovery" in second


def test_foreground_near_duplicate_slug_needs_explicit_override(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)
    la(
        title="better auth jwks poisoning recovery",
        body="Delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    refused = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="User-authored foreground correction may be intentionally close.",
        source="foreground",
    )
    assert refused.startswith("ERR likely_duplicate_lesson")
    assert "new_despite=better-auth-jwks-poisoning-recovery" in refused

    out = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="User-authored foreground correction may be intentionally close.",
        source="foreground",
        new_despite="better-auth-jwks-poisoning-recovery",
    )
    assert out.startswith(
        "ok slug=better-auth-jwks-poisoning-diagnosis-and-recovery"
    )


def test_foreground_semantic_duplicate_is_refused_with_neighbour(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    la(title="serialize emulator boots", body="Boot one emulator at a time.")

    out = la(
        title="one avd at a time",
        body="Start Android virtual devices one by one on shared runners.",
        source="T1",
    )

    assert out.startswith("ERR possible_duplicate_lesson slug=serialize-emulator-boots")
    assert "new_despite=serialize-emulator-boots" in out
    assert pkg["lessons"].count_lessons() == 1
    row = pkg["db"].get_db().execute(
        "SELECT target, summary FROM events WHERE kind='lesson_refused'"
    ).fetchone()
    assert row["target"] == "one-avd-at-a-time"
    assert "reason=possible_duplicate_lesson" in row["summary"]
    assert "neighbour=serialize-emulator-boots" in row["summary"]
    appended = pkg["db"].get_db().execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='lesson_append'"
    ).fetchone()
    assert appended["n"] == 1


def test_foreground_rewrite_of_own_lesson_skips_duplicate_gates(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    la(title="serialize emulator boots", body="Boot one emulator at a time.")
    la(
        title="one avd at a time", body="Start AVDs one by one.",
        new_despite="serialize-emulator-boots",
    )

    out = la(title="one avd at a time", body="Start AVDs one by one, always.")

    assert out.startswith("ok slug=one-avd-at-a-time")
    assert "always" in pkg["path"].read_text()


def test_semantic_ranking_reuses_cached_lesson_vectors(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    ln = _tool(pkg, "lesson_neighbors")
    pkg["lessons"].append_lesson(title="first rule", body="Alpha body.")
    pkg["lessons"].append_lesson(title="second rule", body="Beta body.")
    import threadkeeper.embeddings as embeddings

    encoded: list[int] = []

    def counting_encoder(texts):
        encoded.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(embeddings, "encode_many", counting_encoder)

    ln(title="query one", body="Gamma body.")
    ln(title="query two", body="Delta body.")

    assert encoded == [3, 1]


def test_shadow_lesson_append_semantic_duplicate_patches_incumbent(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")

    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings,
        "encode_many",
        lambda texts: [[1.0, 0.0], [0.86, 0.0]],
    )

    first = la(
        title="android emulator boot lock recovery",
        body=(
            "When multiple Android emulators boot on one runner, serialize "
            "boot and guard lock files before WDA startup."
        ),
        source="shadow",
    )
    assert first.startswith("ok")

    second = la(
        title="ci device startup coordination",
        body=(
            "For CI device pools, serialize AVD boot lock acquisition before "
            "starting WDA to prevent runner deadlocks."
        ),
        source="shadow",
    )

    assert second.startswith("ok slug=android-emulator-boot-lock-recovery")
    assert "dedup=semantic" in second
    assert pkg["lessons"].count_lessons() == 1
    body = pkg["path"].read_text()
    assert "ci-device-startup-coordination" not in body
    assert "Additional evidence:" in body
    assert "serialize AVD boot lock acquisition" in body


def test_absolute_directive_no_longer_bypasses_duplicate_gate(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    la(
        title="module globals for test configuration",
        body=(
            "You can use module globals to pass test configuration between "
            "runner steps when setup is simple."
        ),
        source="shadow",
    )

    out = la(
        title="inject test configuration explicitly",
        body=(
            "Never use module globals to pass test configuration between "
            "runner steps; inject dependencies explicitly instead."
        ),
        source="shadow",
    )

    assert out.startswith("ok slug=module-globals-for-test-configuration")
    assert "dedup=semantic" in out
    assert pkg["lessons"].count_lessons() == 1
    reconciliation = pkg["db"].get_db().execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='lesson_reconciliation'"
    ).fetchone()
    assert reconciliation["n"] == 0


def test_debunk_of_near_duplicate_must_correct_the_existing_lesson(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    la(
        title="shared runner environment state",
        body=(
            "It is safe to keep shared runner environment state throughout "
            "a test workflow."
        ),
        source="shadow",
    )

    out = la(
        title="shared runner environment state is unreliable",
        body=(
            "Keeping shared runner environment state is unreliable; pass the "
            "environment to each command instead."
        ),
        source="shadow",
    )

    assert out.startswith(
        "ERR contradicts_existing_lesson slug=shared-runner-environment-state"
    )
    assert "lesson_patch" in out
    assert pkg["lessons"].count_lessons() == 1
    assert "It is safe to keep" in pkg["path"].read_text()


def test_unrelated_debunk_creates_a_new_lesson(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: [[1.0, 0.0]] + [[0.1, 0.0] for _ in texts[1:]],
    )
    la(
        title="shared runner environment state",
        body="It is safe to keep shared runner environment state.",
        source="shadow",
    )

    out = la(
        title="avoid broken jest watchman cache",
        body="A broken watchman cache is unreliable; reset it before reruns.",
        source="shadow",
    )

    assert out == (
        f"ok slug=avoid-broken-jest-watchman-cache path={pkg['path']}"
    )
    assert pkg["lessons"].count_lessons() == 2


def test_replacing_a_lesson_keeps_its_creator_origin(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    pkg["lessons"].append_lesson(
        title="shadow rule", body="first body", source="shadow",
    )
    monkeypatch.setattr(pkg["lessons"], "WRITE_ORIGIN", "foreground")

    pkg["lessons"].append_lesson(
        title="shadow rule", body="rewritten body", source="T1",
    )

    text = pkg["path"].read_text()
    assert "rewritten body" in text
    assert "origin=shadow_review" in text
    assert "origin=foreground" not in text


def test_backfill_lesson_origins_fixes_loop_sources_once(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    path = pkg["path"]
    path.write_text(
        "# thread-keeper lessons\n\n"
        "<!-- LESSON:BEGIN slug=a ts=1 source=shadow -->\n## a\nA\n"
        "<!-- LESSON:END slug=a -->\n\n"
        "<!-- LESSON:BEGIN slug=b ts=2 source=shadow origin=foreground -->\n## b\nB\n"
        "<!-- LESSON:END slug=b -->\n\n"
        "<!-- LESSON:BEGIN slug=c ts=3 source=curator-consolidate -->\n## c\nC\n"
        "<!-- LESSON:END slug=c -->\n\n"
        "<!-- LESSON:BEGIN slug=d ts=4 source=Tab12 -->\n## d\nD\n"
        "<!-- LESSON:END slug=d -->\n\n"
        "<!-- LESSON:BEGIN slug=e ts=5 source=T9 origin=foreground -->\n## e\nE\n"
        "<!-- LESSON:END slug=e -->\n\n"
    )
    original = path.read_text()
    lessons = pkg["lessons"]

    assert lessons.backfill_lesson_origins(dry_run=True) == {
        "shadow_review": 2, "curator": 1, "foreground": 1,
    }
    assert path.read_text() == original

    assert lessons.backfill_lesson_origins() == {
        "shadow_review": 2, "curator": 1, "foreground": 1,
    }
    origins = {item["slug"]: item["origin"] for item in lessons.iter_lessons()}
    assert origins == {
        "a": "shadow_review", "b": "shadow_review", "c": "curator",
        "d": "foreground", "e": "foreground",
    }
    backup = path.with_name(path.name + lessons.ORIGIN_BACKFILL_BACKUP_SUFFIX)
    assert backup.read_text() == original
    assert lessons.backfill_lesson_origins() == {}
    assert lessons.lesson_protection({"origin": origins["a"]}) == (False, "")


def test_shadow_lesson_append_semantic_borderline_surfaces_duplicate(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")

    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings,
        "encode_many",
        lambda texts: [[1.0, 0.0], [0.80, 0.0]],
    )

    la(
        title="ios permission prompt setup",
        body="Pre-deny camera permission before launching iOS E2E tests.",
        source="shadow",
    )
    out = la(
        title="mobile camera modal preparation",
        body="Prepare camera modal tests by setting iOS permissions first.",
        source="shadow",
    )

    assert out.startswith("ERR possible_duplicate_lesson")
    assert "ios-permission-prompt-setup" in out
    assert pkg["lessons"].count_lessons() == 1
    assert "mobile-camera-modal-preparation" not in pkg["path"].read_text()


def test_shadow_lesson_append_semantic_distinct_creates_new_lesson(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")

    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings,
        "encode_many",
        lambda texts: [[1.0, 0.0], [0.20, 0.0]],
    )

    la(
        title="ios permission prompt setup",
        body="Pre-deny camera permission before launching iOS E2E tests.",
        source="shadow",
    )
    out = la(
        title="github issue claim race",
        body="After posting an issue claim, re-fetch comments before spawning.",
        source="shadow",
    )

    assert out.startswith("ok slug=github-issue-claim-race")
    assert pkg["lessons"].count_lessons() == 2


def test_mcp_lesson_list_returns_summary(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ll = _tool(pkg, "lesson_list")
    assert ll() == "no_lessons"
    la(title="pagination", body="use cursor not offset", source="T1")
    la(title="error-handling", body="wrap external calls", source="T2")
    out = ll()
    assert "total=2" in out
    assert "pagination" in out
    assert "error-handling" in out


def test_lesson_list_query_ranks_relevant_lessons_first(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    lessons = pkg["lessons"]
    lessons.append_lesson(
        title="old simulator network reset", summary="Reset the simulator proxy.",
        body="Reset simulator proxy settings before WDA.",
    )
    lessons.append_lesson(title="newest release notes", body="Write release notes.")
    import threadkeeper.embeddings as embeddings

    def fake_vector(text: str) -> list[float]:
        if "flaky simulator" in text or "simulator proxy" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(
        embeddings, "encode_many", lambda texts: [fake_vector(t) for t in texts],
    )

    out = _tool(pkg, "lesson_list")(k=1, query="flaky simulator network")

    lines = out.splitlines()
    assert "query_mode=semantic" in lines[0]
    assert len(lines) == 2
    assert "old-simulator-network-reset" in lines[1]
    assert "score=1.00" in lines[1]
    assert lines[1].endswith("Reset the simulator proxy.")


def test_lesson_list_query_falls_back_to_lexical_ranking(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    lessons = pkg["lessons"]
    lessons.append_lesson(title="simulator proxy", body="Reset simulator proxy settings.")
    lessons.append_lesson(title="release notes", body="Write release notes.")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)

    out = _tool(pkg, "lesson_list")(k=5, query="simulator proxy broken")

    assert "query_mode=lexical" in out
    assert "simulator-proxy" in out
    assert "release-notes" not in out


def test_mcp_lesson_reads_bump_usage_counters(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ll = _tool(pkg, "lesson_list")
    lg = _tool(pkg, "lesson_get")
    la(title="pagination", body="use cursor not offset", source="T1")
    la(title="error-handling", body="wrap external calls", source="T2")

    ll(k=2)
    lg(slug="pagination")

    conn = pkg["db"].get_db()
    rows = {
        r["slug"]: dict(r)
        for r in conn.execute("SELECT * FROM lesson_usage").fetchall()
    }
    assert rows["pagination"]["view_count"] == 1
    assert rows["pagination"]["use_count"] == 1
    assert rows["pagination"]["last_viewed_at"] is not None
    assert rows["pagination"]["last_used_at"] is not None
    assert rows["error-handling"]["view_count"] == 1
    assert rows["error-handling"]["use_count"] == 0


def test_loop_reads_do_not_bump_usage_counters(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["lessons"].append_lesson(title="pagination", body="use cursor", source="T1")
    ll = _tool(pkg, "lesson_list")
    lg = _tool(pkg, "lesson_get")

    ll(k=5)
    assert "use cursor" in lg(slug="pagination")

    row = pkg["db"].get_db().execute(
        "SELECT use_count, view_count, last_used_at, last_viewed_at "
        "FROM lesson_usage WHERE slug='pagination'"
    ).fetchone()
    assert row is None or tuple(row) == (0, 0, None, None)


def test_mcp_lesson_get_returns_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lg = _tool(pkg, "lesson_get")
    la(title="retry-strategy", body="exponential backoff with jitter")
    out = lg(slug="retry-strategy")
    assert "exponential backoff" in out
    assert lg(slug="does-not-exist").startswith("ERR not_found")


def test_rank_stale_lessons_scores_and_skips_protected(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    now = 1_800_000_000
    old = now - 90 * 86400
    monkeypatch.setattr(pkg["lessons"].time, "time", lambda: old)
    for title in [
        "never pulled",
        "one old pull",
        "pinned lesson",
        "validated lesson",
    ]:
        pkg["lessons"].append_lesson(title=title, body=f"{title} body",
                                     source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_used_at, use_count) "
        "VALUES (?, ?, 'shadow', ?, 1)",
        ("one-old-pull", old, now - 70 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, pinned) "
        "VALUES (?, ?, 'shadow', ?, 1, 1)",
        ("pinned-lesson", old, now - 80 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, tier) "
        "VALUES (?, ?, 'shadow', ?, 1, 'validated')",
        ("validated-lesson", old, now - 80 * 86400),
    )
    conn.commit()

    ranked = pkg["lessons"].rank_stale_lessons(
        conn, now=now, stale_after_days=30, low_pull_count=1,
    )

    assert [r["slug"] for r in ranked] == ["never-pulled", "one-old-pull"]
    assert ranked[0]["decay_score"] < ranked[1]["decay_score"]
    assert all(not r["protected"] for r in ranked)


def test_mcp_lesson_remove_deletes_nonprotected_section(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    lg = _tool(pkg, "lesson_get")
    la(title="stale duplicate", body="old duplicate rule", source="shadow")

    out = lr(slug="stale-duplicate")

    assert out == "ok removed=stale-duplicate"
    assert lg(slug="stale-duplicate").startswith("ERR not_found")
    assert "stale-duplicate" not in pkg["path"].read_text()
    conn = pkg["db"].get_db()
    n_usage = conn.execute(
        "SELECT COUNT(*) FROM lesson_usage WHERE slug='stale-duplicate'"
    ).fetchone()[0]
    assert n_usage == 0
    trash_root = tmp_path / "curator" / "trash"
    artifacts = list(trash_root.glob("*-lesson-stale-duplicate"))
    assert len(artifacts) == 1
    assert (artifacts[0] / "section.md").read_text().startswith(
        "<!-- LESSON:BEGIN slug=stale-duplicate "
    )


def test_lesson_consolidation_rewrites_inbound_wikilinks(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    la(title="umbrella rule", body="new canonical rule", source="shadow")
    la(title="retired rule", body="old duplicate rule", source="shadow")
    la(
        title="consumer rule",
        body="See [[retired-rule]], [[retired-rule|this rule]], and "
        "[[retired-rule#details]].",
        source="shadow",
    )

    out = lr(slug="retired-rule", replacement_slug="umbrella rule")

    assert out == "ok removed=retired-rule inbound_rewritten=lesson:consumer-rule"
    consumer = _tool(pkg, "lesson_get")(slug="consumer-rule")
    assert "[[umbrella-rule]]" in consumer
    assert "[[umbrella-rule|this rule]]" in consumer
    assert "[[umbrella-rule#details]]" in consumer
    assert "[[retired-rule" not in consumer


def test_lesson_remove_reports_dangling_wikilink_sources(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    la(title="retired rule", body="old duplicate rule", source="shadow")
    la(title="consumer rule", body="See [[retired-rule]].", source="shadow")

    out = lr(slug="retired-rule")

    assert out == "ok removed=retired-rule dangling_wikilinks=lesson:consumer-rule"


def test_mcp_lesson_restore_recreates_original_bytes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    restore = _tool(pkg, "lesson_restore")
    la(title="first lesson", body="first body", source="shadow")
    la(title="stale duplicate", body="old duplicate rule", source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "UPDATE lesson_usage SET use_count=3, view_count=4 "
        "WHERE slug='stale-duplicate'"
    )
    conn.commit()
    before = pkg["path"].read_bytes()

    assert lr(slug="stale-duplicate") == "ok removed=stale-duplicate"
    assert restore(slug="stale-duplicate").startswith(
        "ok restored=stale-duplicate"
    )

    assert pkg["path"].read_bytes() == before
    row = conn.execute(
        "SELECT use_count, view_count FROM lesson_usage "
        "WHERE slug='stale-duplicate'"
    ).fetchone()
    assert dict(row) == {"use_count": 3, "view_count": 4}


def test_mcp_lesson_remove_refuses_protected_without_force(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    la(title="human policy", body="keep this", source="T123")

    out = lr(slug="human-policy")

    assert out.startswith("ERR protected_lesson")
    assert "human-policy" in pkg["path"].read_text()
    assert not (tmp_path / "curator" / "trash").exists()
    assert lr(slug="human-policy", force=True) == "ok removed=human-policy"


def test_mcp_lesson_remove_refuses_legacy_empty_source_without_force(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["path"].write_text(
        "# thread-keeper lessons\n\n"
        "<!-- LESSON:BEGIN slug=legacy-empty ts=123 source= -->\n"
        "## legacy-empty\n\n"
        "old body\n"
        "<!-- LESSON:END slug=legacy-empty -->\n",
        encoding="utf-8",
    )
    lr = _tool(pkg, "lesson_remove")

    out = lr(slug="legacy-empty")

    assert out.startswith("ERR protected_lesson")
    assert "reason=unknown_origin" in out
    assert "legacy-empty" in pkg["path"].read_text()


def test_curator_origin_cannot_force_remove_foreground_lesson(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["path"].write_text(
        "# thread-keeper lessons\n\n"
        "<!-- LESSON:BEGIN slug=human-owned ts=123 source=T123 "
        "origin=foreground -->\n"
        "## human-owned\n\n"
        "keep this\n"
        "<!-- LESSON:END slug=human-owned -->\n",
        encoding="utf-8",
    )
    lr = _tool(pkg, "lesson_remove")

    out = lr(slug="human-owned", force=True)

    assert out.startswith("ERR verification_required action=lesson_remove")
    assert "human-owned" in pkg["path"].read_text()


_HUMAN_LESSON = (
    "# thread-keeper lessons\n\n"
    "<!-- LESSON:BEGIN slug=human-owned ts=123 source=T123 "
    "origin=foreground -->\n"
    "## human-owned\n\n"
    "keep this\n"
    "<!-- LESSON:END slug=human-owned -->\n"
)


def _enter_curator_pass(tmp_path, monkeypatch, *, phase: str, scope: tuple = ()):
    snapshot = tmp_path / "curator" / "snapshots" / "pass-1"
    snapshot.mkdir(parents=True)
    monkeypatch.setenv("THREADKEEPER_CURATOR_PASS_ID", "pass-1")
    monkeypatch.setenv("THREADKEEPER_CURATOR_SNAPSHOT_DIR", str(snapshot))
    monkeypatch.setenv("THREADKEEPER_CURATOR_PHASE", phase)
    monkeypatch.setenv("THREADKEEPER_CURATOR_VERIFY_BATCH", "1")
    scope_dir = snapshot / "verify_scope"
    scope_dir.mkdir()
    (scope_dir / "batch-001.json").write_text(
        '{"pass_id": "pass-1", "batch_index": 1, "keys": %s}'
        % str(sorted(scope)).replace("'", '"'),
        encoding="utf-8",
    )


def test_curator_evaluator_may_not_patch_a_protected_lesson(tmp_path, monkeypatch):
    _enter_curator_pass(tmp_path, monkeypatch, phase="evaluate")
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["path"].write_text(_HUMAN_LESSON, encoding="utf-8")

    patched = _tool(pkg, "lesson_patch")(
        slug="human-owned", old_string="keep this", new_string="changed",
    )
    replaced = _tool(pkg, "lesson_append")(title="human-owned", body="changed")

    assert patched.startswith("ERR verification_required action=lesson_patch")
    assert replaced.startswith("ERR verification_required action=lesson_replace")
    assert "keep this" in pkg["path"].read_text()


def test_curator_verifier_removes_a_protected_lesson(tmp_path, monkeypatch):
    _enter_curator_pass(tmp_path, monkeypatch, phase="verify", scope=("human-owned",))
    monkeypatch.setenv("THREADKEEPER_CURATOR_MAX_DESTRUCTIVE_PER_PASS", "5")
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["path"].write_text(_HUMAN_LESSON, encoding="utf-8")

    out = _tool(pkg, "lesson_remove")(slug="human-owned")

    assert out == "ok removed=human-owned"
    assert "human-owned" not in pkg["path"].read_text()


def test_curator_verifier_cannot_remove_a_pinned_lesson(tmp_path, monkeypatch):
    _enter_curator_pass(tmp_path, monkeypatch, phase="verify", scope=("human-owned",))
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["path"].write_text(_HUMAN_LESSON, encoding="utf-8")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO lesson_usage (slug, created_at, pinned) "
        "VALUES ('human-owned', 1, 1)"
    )
    conn.commit()

    out = _tool(pkg, "lesson_remove")(slug="human-owned")
    patched = _tool(pkg, "lesson_patch")(
        slug="human-owned", old_string="keep this", new_string="changed",
    )

    assert out.startswith("ERR protected_lesson slug=human-owned reason=pinned")
    assert patched == "ERR pinned_lesson slug=human-owned"
    assert "keep this" in pkg["path"].read_text()


def test_curator_trash_sweep_removes_expired_artifacts(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    from threadkeeper import trash

    monkeypatch.setattr(trash, "CURATOR_TRASH_TTL_DAYS", 1)
    artifact = tmp_path / "curator" / "trash" / "old-lesson"
    artifact.mkdir(parents=True)
    (artifact / "meta.json").write_text(
        '{"kind":"lesson","slug":"old","created_at":100}\n',
        encoding="utf-8",
    )

    assert trash.sweep_expired_trash(now=100 + 2 * 86400) == 1
    assert not artifact.exists()


def test_iter_lessons_reads_origin_after_a_very_long_slug(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    slug = "a-" + "very-long-lesson-slug-" * 9 + "end"
    pkg["path"].write_text(
        "# thread-keeper lessons\n\n"
        f"<!-- LESSON:BEGIN slug={slug} ts=7 source=shadow origin=shadow_review -->\n"
        f"## {slug}\nbody\n<!-- LESSON:END slug={slug} -->\n",
        encoding="utf-8",
    )

    (item,) = list(pkg["lessons"].iter_lessons())

    assert len(slug) > 200
    assert (item["ts"], item["source"], item["origin"]) == (7, "shadow", "shadow_review")


def test_curator_verifier_only_touches_lessons_its_proposals_name(tmp_path, monkeypatch):
    _enter_curator_pass(tmp_path, monkeypatch, phase="verify", scope=("named-lesson",))
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["lessons"].append_lesson(title="named lesson", body="a", source="shadow")
    pkg["lessons"].append_lesson(title="bystander lesson", body="b", source="shadow")

    refused = _tool(pkg, "lesson_remove")(slug="bystander-lesson")
    created = _tool(pkg, "lesson_append")(title="unnamed umbrella", body="c")

    assert refused.startswith("ERR outside_verify_scope key=bystander-lesson")
    assert created.startswith("ERR outside_verify_scope key=unnamed-umbrella")
    assert _tool(pkg, "lesson_remove")(slug="named-lesson") == "ok removed=named-lesson"


def test_curator_verifier_without_a_scope_file_is_refused(tmp_path, monkeypatch):
    _enter_curator_pass(tmp_path, monkeypatch, phase="verify", scope=("x-lesson",))
    monkeypatch.setenv("THREADKEEPER_CURATOR_VERIFY_BATCH", "7")
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    pkg["lessons"].append_lesson(title="x lesson", body="a", source="shadow")

    out = _tool(pkg, "lesson_remove")(slug="x-lesson")

    assert out.startswith("ERR verify_scope_unavailable key=x-lesson")


def test_evolve_report_applier_cannot_remove_lessons(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="evolve_apply")
    pkg["lessons"].append_lesson(title="loop lesson", body="a", source="shadow")

    out = _tool(pkg, "lesson_remove")(slug="loop-lesson")

    assert out.startswith("ERR verification_required action=lesson_remove")



def _constant_vectors(monkeypatch):
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(
        embeddings, "encode_many", lambda texts: [[1.0, 0.0] for _ in texts],
    )


def test_loop_rewrite_of_a_human_lesson_stays_human_after_backfill(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    pkg["path"].write_text(
        "# thread-keeper lessons\n\n"
        "<!-- LESSON:BEGIN slug=human-rule ts=1 source=T42 origin=foreground -->\n"
        "## human-rule\nkeep\n<!-- LESSON:END slug=human-rule -->\n",
        encoding="utf-8",
    )

    pkg["lessons"].append_lesson(title="human rule", body="rewritten", source="shadow")
    pkg["lessons"].backfill_lesson_origins()

    (item,) = list(pkg["lessons"].iter_lessons())
    assert (item["source"], item["origin"]) == ("T42", "foreground")
    assert pkg["lessons"].lesson_protection(item) == (True, "foreground")


def test_loop_rewrite_of_a_legacy_marker_gets_the_backfilled_origin(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    pkg["path"].write_text(
        "# thread-keeper lessons\n\n"
        "<!-- LESSON:BEGIN slug=legacy-rule ts=1 source=T42 -->\n"
        "## legacy-rule\nold\n<!-- LESSON:END slug=legacy-rule -->\n",
        encoding="utf-8",
    )

    pkg["lessons"].append_lesson(title="legacy rule", body="new", source="shadow")

    (item,) = list(pkg["lessons"].iter_lessons())
    assert item["origin"] == "foreground"


def test_same_slug_loop_rewrite_is_never_merged_into_a_neighbour(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    _constant_vectors(monkeypatch)
    pkg["lessons"].append_lesson(title="rule a", body="Rule A text.", source="shadow")
    pkg["lessons"].append_lesson(title="rule b", body="Rule B text.", source="shadow")

    out = _tool(pkg, "lesson_append")(title="rule a", body="Rule A and B.", source="shadow")

    assert out.startswith("ERR likely_duplicate_lesson slug=rule-b")
    assert "rewriting rule-a would collide" in out
    text = pkg["path"].read_text()
    assert "Rule B text." in text and "Additional evidence" not in text


def test_curator_verifier_writes_a_verified_umbrella_without_the_gate(tmp_path, monkeypatch):
    _enter_curator_pass(
        tmp_path, monkeypatch, phase="verify", scope=("rule-a", "rule-b", "umbrella-rule"),
    )
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="curator")
    _constant_vectors(monkeypatch)
    pkg["lessons"].append_lesson(title="rule a", body="Rule A text.", source="shadow")
    pkg["lessons"].append_lesson(title="rule b", body="Rule B text.", source="shadow")

    out = _tool(pkg, "lesson_append")(
        title="umbrella rule", body="Rule A and rule B.", source="curator",
    )

    assert out.startswith("ok slug=umbrella-rule")


def test_new_despite_accepts_every_neighbour_the_gates_name(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    import threadkeeper.embeddings as embeddings

    def vectors(texts):
        return [[1.0, 0.0] if "semantic twin" in t or "Fresh" in t else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(embeddings, "encode_many", vectors)
    la = _tool(pkg, "lesson_append")
    la(title="semantic twin", body="Fresh evidence about the same failure.")
    la(title="better auth jwks poisoning recovery", body="Unrelated body.", new_despite="semantic-twin")

    out = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="Fresh rule.",
        new_despite="semantic-twin, better-auth-jwks-poisoning-recovery",
    )

    assert out.startswith("ok slug=better-auth-jwks-poisoning-diagnosis-and-recovery")


def test_loop_writer_cannot_skip_the_gates_with_a_foreground_source(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="shadow_review")
    _constant_vectors(monkeypatch)
    la = _tool(pkg, "lesson_append")
    la(title="original rule", body="Original.", source="shadow")

    out = la(
        title="sneaky twin", body="Same rule.", source="foreground",
        new_despite="original-rule",
    )

    assert out.startswith("ok slug=original-rule")
    assert "dedup=semantic" in out
    assert pkg["lessons"].count_lessons() == 1


def test_lesson_list_lexical_query_matches_any_script(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    lessons = pkg["lessons"]
    lessons.append_lesson(
        title="naris branch rule", body="Ветки задач создаются от свежего master.",
    )
    lessons.append_lesson(title="unrelated", body="Release notes go in CHANGELOG.")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)

    out = _tool(pkg, "lesson_list")(k=5, query="как создавать ветки от master")

    assert "naris-branch-rule" in out
    assert "unrelated" not in out


def test_lesson_list_lexical_query_favours_lessons_that_cover_the_query(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    lessons = pkg["lessons"]
    lessons.append_lesson(
        title="long relevant",
        body="Simulator proxy reset. " + " ".join(f"detail{i}" for i in range(60)),
    )
    lessons.append_lesson(title="short partial", body="Proxy only.")
    import threadkeeper.embeddings as embeddings

    monkeypatch.setattr(embeddings, "encode_many", lambda texts: None)

    lines = _tool(pkg, "lesson_list")(k=2, query="simulator proxy reset").splitlines()

    assert "long-relevant" in lines[1]


def test_lesson_vectors_persist_and_resume_after_a_failed_chunk(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    for i in range(40):
        pkg["lessons"].append_lesson(title=f"lesson {i}", body=f"Body number {i}.")
    import threadkeeper.embeddings as embeddings
    import threadkeeper.tools.lessons as lesson_tools

    batches: list[int] = []

    def encoder(texts):
        batches.append(len(texts))
        if len(batches) == 2:
            return None
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(embeddings, "encode_many", encoder)

    first = _tool(pkg, "lesson_list")(k=1, query="body")
    lesson_tools._lesson_vector_cache.clear()
    second = _tool(pkg, "lesson_list")(k=1, query="body")

    assert "query_mode=lexical" in first
    assert "query_mode=semantic" in second
    assert batches == [lesson_tools._LESSON_ENCODE_CHUNK, 9, 9]


def test_lesson_encoding_stops_when_its_time_budget_runs_out(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    for i in range(40):
        pkg["lessons"].append_lesson(title=f"lesson {i}", body=f"Body number {i}.")
    import threadkeeper.embeddings as embeddings
    import threadkeeper.tools.lessons as lesson_tools

    batches: list[int] = []
    monkeypatch.setattr(
        embeddings, "encode_many",
        lambda texts: batches.append(len(texts)) or [[1.0, 0.0] for _ in texts],
    )

    class SlowClock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            cls.now += 5.0
            return cls.now

        time = staticmethod(time.time)

    monkeypatch.setattr(lesson_tools, "time", SlowClock)

    out = _tool(pkg, "lesson_list")(k=1, query="body")

    assert "query_mode=lexical" in out
    assert batches == [lesson_tools._LESSON_ENCODE_CHUNK]
