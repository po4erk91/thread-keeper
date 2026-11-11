"""Lesson audit evidence is reproducible, private, and version bound."""
import copy
import hashlib
import json

import pytest


def _item(slug="alpha", body="Review release evidence before publishing.", **kwargs):
    return {
        "slug": slug, "body": body, "ts": 123, "source": "shadow",
        "origin": "shadow_review", "usage": {}, **kwargs,
    }


def _audit(items):
    from threadkeeper.curator_audit import build_curator_audit
    return build_curator_audit({"schema_version": 2, "skills": [{"name": "skill-target"}]}, items)


def _serialized(manifest):
    from threadkeeper.skill_audit import serialized_skill_audit
    return serialized_skill_audit(manifest)


def _slice(manifest, slugs=("alpha",)):
    from threadkeeper.curator_audit import build_batch_audit_slice
    return build_batch_audit_slice(
        manifest, pass_id="pass", batch_index=1, batch_total=1,
        inventory_fingerprint="f" * 64, canonical_manifest_sha256="a" * 64,
        assigned_skill_names=[], assigned_lesson_slugs=slugs,
    )


def test_lesson_hashes_and_manifest_serialization_are_deterministic():
    from threadkeeper.lesson_freshness import lesson_revision
    items = [_item("beta"), _item("alpha", usage={"last_used_at": 50, "last_viewed_at": 60})]
    first = _audit(items)
    second = _audit(list(reversed(items)))
    assert _serialized(first) == _serialized(second)
    assert first["schema_version"] == 3
    records = first["lesson_audit"]["lessons"]
    assert [record["slug"] for record in records] == ["alpha", "beta"]
    assert records[0]["body_sha256"] == hashlib.sha256(items[1]["body"].encode()).hexdigest()
    assert records[0]["revision"] == lesson_revision(items[1])
    assert records[0]["timestamp"] == 123
    assert records[0]["usage"]["last_used_at"] == 50
    assert records[0]["usage"]["last_viewed_at"] == 60


def test_body_edit_changes_revision_and_manifest_digest_but_usage_does_not_change_revision():
    before = _audit([_item()])
    after = _audit([_item(body="Changed release rule.")])
    digest = lambda manifest: hashlib.sha256(_serialized(manifest).encode()).hexdigest()
    assert digest(before) != digest(after)
    for key in ("body_sha256", "normalized_body_sha256", "revision"):
        assert before["lesson_audit"]["lessons"][0][key] != after["lesson_audit"]["lessons"][0][key]
    usage = _audit([_item(usage={"view_count": 10})])
    assert before["lesson_audit"]["lessons"][0]["revision"] == usage["lesson_audit"]["lessons"][0]["revision"]


def test_links_include_adjacency_skill_targets_aliases_and_unresolved_targets():
    audit = _audit([
        _item("alpha", "[[beta]] [[beta|alias]] [[missing]] [[skill-target]] [[alpha]]"),
        _item("beta", "[[zebra]] [[missing]]"),
    ])["lesson_audit"]
    alpha, beta = audit["lessons"]
    assert alpha["outgoing_links"] == ["alpha", "beta", "missing", "skill-target"]
    assert alpha["adjacent_lessons"] == ["beta"]
    assert beta["adjacent_lessons"] == ["alpha"]
    assert alpha["unresolved_links"] == ["missing"]
    assert beta["unresolved_links"] == ["missing", "zebra"]


def test_exact_normalized_and_near_duplicate_groups_are_sorted_and_body_free():
    audit = _audit([
        _item("beta", "Check release evidence before publishing"),
        _item("alpha", "Check release evidence before publishing"),
        _item("gamma", "CHECK release evidence, before publishing!"),
        _item("delta", "Check release evidence before publishing safely"),
    ])["lesson_audit"]
    assert audit["exact_duplicate_groups"][0]["lessons"] == ["alpha", "beta"]
    assert audit["normalized_duplicate_groups"][0]["lessons"] == ["alpha", "beta", "gamma"]
    assert any(pair["left"] == "alpha" and pair["right"] == "delta" for pair in audit["near_duplicate_candidates"])
    assert all(set(pair) == {"left", "right", "token_jaccard", "status"} for pair in audit["near_duplicate_candidates"])


def test_protected_lesson_body_and_private_provenance_paths_never_enter_audit():
    item = _item(
        body="PRIVATE_BODY /Users/private/project.md C:\\private\\project [[skill-target]]",
        source="/Users/private/source.md", origin="/private/origin",
        usage={"pinned": 1, "source": "C:\\private\\source", "tier": "validated"},
    )
    manifest = _audit([item])
    encoded = _serialized(manifest)
    for private in ("PRIVATE_BODY", "/Users/private", "/private/origin", "C:\\private"):
        assert private not in encoded
    record = manifest["lesson_audit"]["lessons"][0]
    assert record["protected"] is True
    assert record["protection_reason"] == "pinned"
    assert record["provenance"]["source_kind"] == "redacted"
    assert record["provenance"]["origin"] == "unknown"
    assert record["provenance"]["source_sha256"] == hashlib.sha256(item["source"].encode()).hexdigest()


def test_lesson_slices_include_only_assigned_records_and_related_evidence():
    manifest = _audit([_item("alpha"), _item("beta"), _item("unrelated", "Another rule entirely")])
    sliced = _slice(manifest)
    assert sliced["schema_version"] == 2
    assert sliced["no_skills"] is True
    assert sliced["assigned_lesson_slugs"] == ["alpha"]
    assert [record["slug"] for record in sliced["lesson_audit"]["lessons"]] == ["alpha"]
    assert sliced["lesson_audit"]["exact_duplicate_groups"][0]["lessons"] == ["alpha", "beta"]
    assert "unrelated" not in json.dumps(sliced)
    with pytest.raises(ValueError, match="lesson_audit_scope_missing"):
        _slice(manifest, ["missing"])


def test_report_receipt_records_changed_and_removed_lessons_and_rejects_other_revisions():
    from threadkeeper.curator_audit import audit_receipt, report_audit_header, report_matches_audit
    manifest = _slice(_audit([_item("alpha"), _item("beta")]), ["alpha", "beta"])
    receipt = audit_receipt(manifest, [_item("alpha", "A new rule")])
    assert [change["slug"] for change in receipt["lesson_revision_changes"]] == ["alpha", "beta"]
    assert receipt["lesson_revision_changes"][1]["current_revision"] is None
    report = report_audit_header(receipt) + "CURATOR_PASS_COMPLETE\n"
    assert report_matches_audit(report, manifest)
    assert not report_matches_audit("CURATOR_PASS_COMPLETE", manifest)
    for key, value in (("sha256", "b" * 64), ("schema_version", 2), ("reviewed_revisions", {})):
        changed = {**receipt, key: value}
        assert not report_matches_audit(report_audit_header(changed), manifest)


@pytest.mark.parametrize("version", [1, 2])
def test_older_manifests_keep_their_original_slice_contract(version):
    from threadkeeper.skill_audit import build_batch_skill_audit_slice
    manifest = {"schema_version": version, "skills": []}
    sliced = _slice(manifest, [])
    assert sliced == build_batch_skill_audit_slice(
        manifest, pass_id="pass", batch_index=1, batch_total=1,
        inventory_fingerprint="f" * 64, canonical_manifest_sha256="a" * 64,
        assigned_skill_names=[],
    )


@pytest.mark.parametrize("version", [None, True, "3", 0, 4])
def test_unknown_manifest_versions_fail_clearly(version):
    from threadkeeper.curator_audit import validate_manifest_version
    with pytest.raises(ValueError, match="unsupported_curator_audit_version"):
        validate_manifest_version({"schema_version": version})


def test_promotion_sources_are_part_of_frozen_lesson_scope():
    from threadkeeper.curator_audit import batch_lesson_slugs
    assert batch_lesson_slugs(
        "- LESSON beta [PROTECTED]\n    lessons: gamma, alpha\n- LESSON beta\n"
    ) == ["alpha", "beta", "gamma"]


def test_audit_builder_does_not_change_original_skill_manifest():
    from threadkeeper.curator_audit import build_curator_audit
    skills = {"schema_version": 2, "skills": []}
    original = copy.deepcopy(skills)
    build_curator_audit(skills, [_item()])
    assert skills == original
