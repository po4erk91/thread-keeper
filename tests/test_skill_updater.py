from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest


def _bootstrap(tmp_path, monkeypatch, *, interval="302400", sources=""):
    claude_root = tmp_path / "claude_skills"
    codex_home = tmp_path / "codex_home"
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "CLAUDE_SKILLS_DIR": str(claude_root),
        "CODEX_HOME": str(codex_home),
        "THREADKEEPER_SKILL_UPDATE_INTERVAL_S": interval,
        "THREADKEEPER_SKILL_UPDATE_SOURCES": sources,
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_THREAD_JANITOR_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    claude_root.mkdir(parents=True, exist_ok=True)
    (codex_home / "skills").mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, skill_updater

    return {
        "db": db,
        "skill_updater": skill_updater,
        "claude_root": claude_root,
        "codex_root": codex_home / "skills",
    }


def _skill_body(name: str, marker: str) -> str:
    return (
        "---\n"
        f"name: \"{name}\"\n"
        "description: \"Use when testing skill updater behavior.\"\n"
        "---\n\n"
        f"# {name}\n\n{marker}\n"
    )


def _write_skill(root: Path, name: str, marker: str, mtime: int | None = None) -> Path:
    sdir = root / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text(_skill_body(name, marker), encoding="utf-8")
    if mtime is not None:
        for path in [sdir, sdir / "SKILL.md"]:
            os.utime(path, (mtime, mtime))
    return sdir


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")

    assert pkg["skill_updater"].run_skill_update_pass() == "disabled"


def test_force_pass_imports_newer_local_skill_copy(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["claude_root"], "demo-skill", "old copy", mtime=100)
    _write_skill(pkg["codex_root"], "demo-skill", "new copy", mtime=200)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "new copy" in (
        pkg["claude_root"] / "demo-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "new copy" in (
        pkg["codex_root"] / "demo-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_force_pass_imports_skill_installed_only_in_non_primary_root(
    tmp_path,
    monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "codex-only", "installed in codex", mtime=200)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "installed in codex" in (
        pkg["claude_root"] / "codex-only" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_source_tracked_skill_updates_and_mirrors(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    local = _write_skill(pkg["claude_root"], "remote-skill", "v1", mtime=100)
    old_hash = updater._tree_sha256(local)
    source = updater.GithubSource(
        repo="example/skills",
        ref="main",
        path="skills/remote-skill",
    )
    (local / updater.SOURCE_FILE).write_text(
        json.dumps({
            "type": "github",
            "repo": source.repo,
            "ref": source.ref,
            "path": source.path,
            "tree_sha256": old_hash,
            "lock_version": 2,
            "verification_policy": "allow_unsigned",
        }),
        encoding="utf-8",
    )
    remote = _write_skill(tmp_path / "remote_repo" / "skills", "remote-skill", "v2")
    remote_skill = updater.RemoteSkill(
        name="remote-skill",
        source=source,
        path=remote,
        tree_sha256=updater._tree_sha256(remote),
        resolved_commit="a" * 40,
    )

    monkeypatch.setattr(
        updater,
        "_build_remote_index",
        lambda sources, tmp_root: (
            {(source.repo, source.ref, source.path): remote_skill},
            {"remote-skill": remote_skill},
            [],
        ),
    )

    out = updater.run_skill_update_pass(force=True)

    assert "remote_updated=1" in out
    assert "v2" in (pkg["claude_root"] / "remote-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "v2" in (pkg["codex_root"] / "remote-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    meta = json.loads((pkg["claude_root"] / "remote-skill" / updater.SOURCE_FILE).read_text(
        encoding="utf-8"
    ))
    assert meta["tree_sha256"] == remote_skill.tree_sha256


def test_source_tracked_skill_skips_local_changes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    local = _write_skill(pkg["claude_root"], "remote-skill", "local edit")
    source = updater.GithubSource(
        repo="example/skills",
        ref="main",
        path="skills/remote-skill",
    )
    (local / updater.SOURCE_FILE).write_text(
        json.dumps({
            "type": "github",
            "repo": source.repo,
            "ref": source.ref,
            "path": source.path,
            "tree_sha256": "previous-upstream-hash",
        }),
        encoding="utf-8",
    )
    remote = _write_skill(tmp_path / "remote_repo" / "skills", "remote-skill", "v2")
    remote_skill = updater.RemoteSkill(
        name="remote-skill",
        source=source,
        path=remote,
        tree_sha256=updater._tree_sha256(remote),
        resolved_commit="a" * 40,
    )
    monkeypatch.setattr(
        updater,
        "_build_remote_index",
        lambda sources, tmp_root: (
            {(source.repo, source.ref, source.path): remote_skill},
            {"remote-skill": remote_skill},
            [],
        ),
    )

    out = updater.run_skill_update_pass(force=True)

    assert "remote_skipped=1" in out
    assert "local edit" in (
        pkg["claude_root"] / "remote-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def _record_skill_delete(pkg, name: str, at: int) -> None:
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('pytest', 'skill_delete', ?, 'trash=x', ?)",
        (name, at),
    )
    conn.commit()


def test_deleted_skill_is_not_revived_from_an_older_mirror(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "deleted-junk", "stale mirror", mtime=200)
    _record_skill_delete(pkg, "deleted-junk", at=300)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "removed_strays=1" in out
    assert "local_updates=0" in out
    assert not (pkg["claude_root"] / "deleted-junk").exists()
    assert not (pkg["codex_root"] / "deleted-junk").exists()


def test_reinstall_after_deletion_is_still_imported(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "reinstalled", "fresh install", mtime=400)
    _record_skill_delete(pkg, "reinstalled", at=300)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "fresh install" in (
        pkg["claude_root"] / "reinstalled" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_invalid_primary_skill_is_not_mirrored(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "broken-skill", "older valid mirror", mtime=100)
    broken = pkg["claude_root"] / "broken-skill"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
    for path in (broken, broken / "SKILL.md"):
        os.utime(path, (300, 300))

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "conflicts=1" in out
    assert "older valid mirror" in (
        pkg["codex_root"] / "broken-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_removing_a_stray_keeps_a_backup(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "old-copy", "stale mirror", mtime=200)
    _record_skill_delete(pkg, "old-copy", at=300)
    backups: list[str] = []
    monkeypatch.setattr(
        pkg["skill_updater"], "_backup_skill_dir",
        lambda path: backups.append(Path(path).name),
    )

    pkg["skill_updater"].run_skill_update_pass(force=True)

    assert backups == ["old-copy"]
    assert not (pkg["codex_root"] / "old-copy").exists()


COMMIT = "a" * 40


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def _tracked_update(tmp_path, monkeypatch, *, policy="require_signed"):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    local = _write_skill(pkg["claude_root"], "signed-skill", "v1", mtime=100)
    remote = _write_skill(tmp_path / "repo" / "skills", "signed-skill", "v2")
    (remote / "references").mkdir()
    (remote / "references" / "guide.txt").write_text("signed reference")
    record = {"type": "github", "repo": "example/skills", "ref": "main",
              "path": "skills/signed-skill", "tree_sha256": updater._tree_sha256(local),
              "lock_version": 2, "verification_policy": policy}
    source = updater.GithubSource(record["repo"], record["ref"], record["path"])

    def index(sources, tmp_root):
        skill = updater.RemoteSkill("signed-skill", source, remote,
                                    updater._tree_sha256(remote), COMMIT)
        return {(source.repo, source.ref, source.path): skill}, {skill.name: skill}, []

    monkeypatch.setattr(updater, "_build_remote_index", index)
    return pkg, local, remote, record


def _save_record(pkg, local, record):
    (local / pkg["skill_updater"].SOURCE_FILE).write_text(json.dumps(record))
    mirror = pkg["codex_root"] / local.name
    shutil.copytree(local, mirror, dirs_exist_ok=True)


def _sign_skill(tmp_path, remote, *, signer="publisher", ignore_paths=()):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from model_signing import hashing, signing

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, signer)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
            .sign(key, hashes.SHA256()))
    key_file = tmp_path / f"{signer}.key"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    cert_file = tmp_path / f"{signer}.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config = hashing.Config().set_ignored_paths(paths=[Path("skill.oms.sig"), *ignore_paths],
                                               ignore_git_paths=False)
    signing.Config().use_certificate_signer(
        private_key=key_file, signing_certificate=cert_file, certificate_chain=[cert_file],
    ).set_hashing_config(config).sign(remote, remote / "skill.oms.sig")
    return {"type": "certificate", "identity": cert.fingerprint(hashes.SHA256()).hex(),
            "certificate_chain": str(cert_file)}


def test_signed_update_verifies_whole_tree_and_keeps_backup(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    _save_record(pkg, local, record)
    old = _snapshot(local)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "remote_updated=1" in out
    assert "verified:1" in out
    installed = json.loads((local / pkg["skill_updater"].SOURCE_FILE).read_text())
    assert installed["resolved_commit"] == COMMIT
    assert installed["tree_sha256"] == pkg["skill_updater"]._tree_sha256(remote)
    assert installed["expected_signer"] == record["expected_signer"]
    assert installed["last_verification"]["result"] == "verified"
    assert _snapshot(local) == _snapshot(pkg["codex_root"] / local.name)
    for root in pkg["skill_updater"]._skill_roots():
        assert _snapshot(local) == _snapshot(root / local.name)
    backups = list((tmp_path / "skill-update-backups").iterdir())
    assert len(backups) == 1
    assert _snapshot(backups[0]) == old


@pytest.mark.parametrize("failure", ["missing", "invalid", "changed_signer", "changed_file",
                                    "extra_file", "missing_signer", "upstream_lock"])
def test_refused_signature_changes_no_root(tmp_path, monkeypatch, failure):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    if failure == "missing":
        (remote / "skill.oms.sig").unlink()
    elif failure == "invalid":
        bundle = json.loads((remote / "skill.oms.sig").read_text())
        bundle["dsseEnvelope"]["signatures"][0]["sig"] = "AAAA"
        (remote / "skill.oms.sig").write_text(json.dumps(bundle))
    elif failure == "changed_signer":
        _sign_skill(tmp_path, remote, signer="other-publisher")
    elif failure == "changed_file":
        (remote / "references" / "guide.txt").write_text("changed after signing")
    elif failure == "extra_file":
        (remote / "extra.py").write_text("unsigned code")
    elif failure == "missing_signer":
        record.pop("expected_signer")
    elif failure == "upstream_lock":
        (remote / pkg["skill_updater"].SOURCE_FILE).write_text('{"verification_policy":"allow_unsigned"}')
    _save_record(pkg, local, record)
    # Even an older, different mirror must stay untouched during refusal.
    _write_skill(pkg["codex_root"], local.name, "older mirror", mtime=50)
    roots = pkg["skill_updater"]._skill_roots()
    before = [_snapshot(root) for root in roots]

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "remote_updated=0" in out
    assert "errors=1" in out
    expected = {"missing": "signature_missing", "invalid": "signature_invalid",
                "changed_signer": "signer_changed", "changed_file": "signature_invalid",
                "extra_file": "digest_mismatch", "missing_signer": "signer_required",
                "upstream_lock": "upstream_lock_refused"}
    assert f"{expected[failure]}:1" in out
    assert [_snapshot(root) for root in roots] == before
    assert not (tmp_path / "skill-update-backups").exists()


def test_legacy_unsigned_source_requires_explicit_policy(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record.pop("lock_version")
    record["tree_sha256"] = pkg["skill_updater"]._legacy_tree_sha256(local)
    record.pop("verification_policy")
    _save_record(pkg, local, record)
    before = _snapshot(local)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "policy_required:1" in out
    assert _snapshot(local) == before
    record["verification_policy"] = "allow_unsigned"
    _save_record(pkg, local, record)
    out = pkg["skill_updater"].run_skill_update_pass(force=True)
    assert "unsigned_allowed:1" in out
    assert "remote_updated=1" in out
    lock = json.loads((local / pkg["skill_updater"].SOURCE_FILE).read_text())
    assert lock["lock_version"] == 2
    assert lock["verification_policy"] == "allow_unsigned"
    assert lock["last_verification"]["result"] == "unsigned_allowed"


def test_partial_directory_signature_is_refused(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    (remote / "extra.py").write_text("unsigned code")
    record["expected_signer"] = _sign_skill(tmp_path, remote, ignore_paths=[Path("extra.py")])
    _save_record(pkg, local, record)
    before = _snapshot(local)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "digest_mismatch:1" in out
    assert _snapshot(local) == before


def test_verified_update_preserves_local_edits_in_a_mirror(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    _save_record(pkg, local, record)
    _write_skill(pkg["codex_root"], local.name, "local mirror edit", mtime=500)
    roots = [pkg["claude_root"], pkg["codex_root"]]
    before = [_snapshot(root) for root in roots]

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "remote_skipped=1" in out
    assert "verified:1" in out
    assert [_snapshot(root) for root in roots] == before
    assert not (tmp_path / "skill-update-backups").exists()


def test_unchanged_signed_tree_is_reverified_and_lock_is_mirrored(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    shutil.copytree(remote, local, dirs_exist_ok=True)
    record["tree_sha256"] = pkg["skill_updater"]._tree_sha256(local)
    _save_record(pkg, local, record)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "remote_no_update=1" in out
    assert "verified:1" in out
    lock = json.loads((local / pkg["skill_updater"].SOURCE_FILE).read_text())
    assert lock["resolved_commit"] == COMMIT
    assert _snapshot(local) == _snapshot(pkg["codex_root"] / local.name)


def test_signed_source_cannot_downgrade_to_unsigned(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch, policy="allow_unsigned")
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    (remote / "skill.oms.sig").unlink()
    _save_record(pkg, local, record)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "signature_missing:1" in out
    assert "remote_updated=0" in out


def test_missing_verifier_refuses_signed_update(tmp_path, monkeypatch):
    import builtins

    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    _save_record(pkg, local, record)
    original_import = builtins.__import__

    def import_without_verifier(name, *args, **kwargs):
        if name == "model_signing":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_verifier)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "verifier_unavailable:1" in out
    assert "remote_updated=0" in out


@pytest.mark.parametrize("sha", [COMMIT, "short", "b" * 40])
def test_commit_resolution_checks_full_pinned_sha(tmp_path, monkeypatch, sha):
    from io import BytesIO

    updater = _bootstrap(tmp_path, monkeypatch)["skill_updater"]
    source = updater.GithubSource("example/skills", COMMIT, "skills")
    urls = []

    def response(req, timeout):
        urls.append(req.full_url)
        return BytesIO(json.dumps({"sha": sha}).encode())

    monkeypatch.setattr(updater.request, "urlopen", response)
    if sha == COMMIT:
        assert updater._resolve_commit(source) == COMMIT
    else:
        with pytest.raises(ValueError, match="commit_mismatch"):
            updater._resolve_commit(source)
    assert urls == [f"https://api.github.com/repos/example/skills/commits/{COMMIT}"]


def test_mutable_ref_is_url_encoded(tmp_path, monkeypatch):
    from io import BytesIO

    updater = _bootstrap(tmp_path, monkeypatch)["skill_updater"]
    urls = []

    def response(req, timeout):
        urls.append(req.full_url)
        return BytesIO(json.dumps({"sha": COMMIT}).encode())

    monkeypatch.setattr(updater.request, "urlopen", response)
    updater._resolve_commit(updater.GithubSource("example/skills", "release/v1", "skills"))
    assert urls[0].endswith("/commits/release%2Fv1")


@pytest.mark.parametrize("layout", ["valid", "wrong_commit", "escape", "symlink"])
def test_archive_matches_resolved_commit_before_extraction(tmp_path, monkeypatch, layout):
    from io import BytesIO
    import stat
    import zipfile

    updater = _bootstrap(tmp_path, monkeypatch)["skill_updater"]
    source = updater.GithubSource("example/skills", "main", "skills")
    archive = BytesIO()
    top = f"skills-{'b' * 40 if layout == 'wrong_commit' else COMMIT}"
    with zipfile.ZipFile(archive, "w") as zf:
        name = f"{top}/SKILL.md"
        if layout == "escape":
            name = f"{top}/../../escaped"
        info = zipfile.ZipInfo(name)
        if layout == "symlink":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "payload")
    urls = []

    def response(req, timeout):
        urls.append(req.full_url)
        return BytesIO(archive.getvalue())

    monkeypatch.setattr(updater.request, "urlopen", response)
    dest = tmp_path / "download"
    dest.mkdir()
    if layout == "valid":
        assert updater._download_repo_zip(source, dest, COMMIT) == dest / top
        assert (dest / top / "SKILL.md").read_text() == "payload"
    else:
        with pytest.raises(ValueError):
            updater._download_repo_zip(source, dest, COMMIT)
        assert not (dest / top).exists()
    assert not (tmp_path / "escaped").exists()
    assert urls == [f"https://codeload.github.com/example/skills/zip/{COMMIT}"]


def test_fetch_error_telemetry_redacts_exception_details(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, sources="example/skills:skills")
    secret = f"{tmp_path}/private ghp_" + "a" * 36

    def fail(source):
        raise RuntimeError(secret)

    monkeypatch.setattr(pkg["skill_updater"], "_resolve_commit", fail)
    out = pkg["skill_updater"].run_skill_update_pass(force=True)
    assert "source_fetch_failed:1" in out
    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='skill_update_pass' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["summary"] == out
    assert str(tmp_path) not in out and "ghp_" not in out


@pytest.mark.parametrize("content", [".DS_Store", ".git/config", "__pycache__/code.pyc", "scripts/run.py"])
def test_whole_directory_digest_includes_supporting_and_hidden_files(tmp_path, monkeypatch, content):
    updater = _bootstrap(tmp_path, monkeypatch)["skill_updater"]
    skill = _write_skill(tmp_path / "root", "digest-skill", "content")
    before = updater._tree_sha256(skill)
    added = skill / content
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("changed content")
    assert updater._tree_sha256(skill) != before


@pytest.mark.parametrize("policy", [None, "unknown", [], {}])
def test_malformed_policy_refuses_without_changing_the_lock(tmp_path, monkeypatch, policy):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch, policy=policy)
    _save_record(pkg, local, record)
    before = _snapshot(local)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "policy_required:1" in out
    assert _snapshot(local) == before


@pytest.mark.parametrize("signer", [[], {}, False, ""])
def test_malformed_signer_does_not_enable_unsigned_updates(tmp_path, monkeypatch, signer):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch, policy="allow_unsigned")
    record["expected_signer"] = signer
    _save_record(pkg, local, record)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "signature_missing:1" in out
    assert "remote_updated=0" in out


def test_inferred_source_does_not_enroll_its_own_policy(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    monkeypatch.setattr(pkg["skill_updater"], "SKILL_UPDATE_SOURCES", "example/skills:skills")
    (remote / pkg["skill_updater"].SOURCE_FILE).write_text(json.dumps(record))
    before = _snapshot(local)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "upstream_lock_refused:1" in out
    assert _snapshot(local) == before


def test_sigstore_verifier_receives_both_expected_identity_and_issuer(tmp_path, monkeypatch):
    from model_signing import verifying

    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    _sign_skill(tmp_path, remote)
    record["expected_signer"] = {"type": "sigstore", "identity": "publisher@example.com",
                                  "issuer": "https://accounts.example.com"}
    _save_record(pkg, local, record)
    received = []
    original = verifying.Config

    class Verifier(original):
        def use_sigstore_verifier(self, **kwargs):
            received.append(kwargs)
            return self

        def verify(self, model_path, signature_path):
            assert model_path == remote
            assert signature_path == remote / "skill.oms.sig"
            raise ValueError("unexpected signer")

    monkeypatch.setattr(verifying, "Config", Verifier)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert received == [{"identity": "publisher@example.com", "oidc_issuer": "https://accounts.example.com"}]
    assert "signature_invalid:1" in out
    assert "remote_updated=0" in out


def test_allow_unsigned_does_not_bypass_present_signature(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch, policy="allow_unsigned")
    (remote / "skill.oms.sig").write_text("invalid signature")
    _save_record(pkg, local, record)
    before = _snapshot(local)

    assert "remote_updated=0" in pkg["skill_updater"].run_skill_update_pass(force=True)
    assert _snapshot(local) == before


def test_verification_telemetry_contains_no_private_values(tmp_path, monkeypatch):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch)
    record["expected_signer"] = _sign_skill(tmp_path, remote)
    _save_record(pkg, local, record)
    pkg["skill_updater"].run_skill_update_pass(force=True)

    row = pkg["db"].get_db().execute(
        "SELECT summary FROM events WHERE kind='skill_update_pass' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "verified:1" in row["summary"]
    assert str(tmp_path) not in row["summary"]
    assert record["expected_signer"]["identity"] not in row["summary"]


def test_mutable_ref_downloads_resolved_commit_once(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    source = updater.GithubSource("example/skills", "main", "skills")
    resolved, fetched = [], []

    def resolve(src):
        resolved.append(src.ref)
        return COMMIT

    def download(src, dest, commit):
        fetched.append(commit)
        _write_skill(dest / "skills", "demo-skill", "v2")
        return dest

    monkeypatch.setattr(updater, "_resolve_commit", resolve)
    monkeypatch.setattr(updater, "_download_repo_zip", download)
    exact, _, errors = updater._build_remote_index([source, source], tmp_path / "download")

    assert errors == []
    assert resolved == ["main"]
    assert fetched == [COMMIT]
    assert exact[(source.repo, source.ref, "skills/demo-skill")].resolved_commit == COMMIT


@pytest.mark.parametrize("failure", ["commit", "digest", "same_commit_digest"])
def test_lock_mismatch_refuses_before_replacement(tmp_path, monkeypatch, failure):
    pkg, local, remote, record = _tracked_update(tmp_path, monkeypatch, policy="allow_unsigned")
    updater = pkg["skill_updater"]
    ref = "b" * 40 if failure == "commit" else "main"
    source = updater.GithubSource(record["repo"], ref, record["path"])
    if failure == "same_commit_digest":
        record["resolved_commit"] = COMMIT
    skill = updater.RemoteSkill(local.name, source, remote,
                                "wrong" if failure == "digest" else updater._tree_sha256(remote),
                                COMMIT)
    _save_record(pkg, local, record)
    before = _snapshot(local)

    result, verification = updater._apply_remote_update(local, skill, inferred=False)

    assert result.startswith("error_")
    assert verification in {"commit_mismatch", "digest_mismatch"}
    assert _snapshot(local) == before
