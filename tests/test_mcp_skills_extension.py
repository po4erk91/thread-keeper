"""Protocol-level coverage for the stable MCP Skills extension (#336)."""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.shared.exceptions import MCPError


_STABLE_PROTOCOL = "2026-07-28"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _write_skill(pkg, name: str, *, extra_frontmatter: str = ""):
    skill_dir = pkg["config"].CLAUDE_SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} description\n"
        f"{extra_frontmatter}"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _call_extension(pkg, method: str, params):
    entry = pkg["mcp"]._lowlevel_server._request_handlers[method]
    return asyncio.run(
        entry.handler(SimpleNamespace(protocol_version=_STABLE_PROTOCOL), params)
    )


def test_discovery_advertises_stable_skills_extension_without_changing_core_contracts(fresh_mp):
    from mcp.server.lowlevel.server import NotificationOptions
    from threadkeeper.mcp_skills import SKILLS_EXTENSION_ID

    server = fresh_mp["mcp"]._lowlevel_server
    caps = server.get_capabilities(
        NotificationOptions(), {}, protocol_version=_STABLE_PROTOCOL,
    )

    assert caps.extensions == {SKILLS_EXTENSION_ID: {}}
    assert caps.resources is not None
    assert caps.prompts is not None
    assert caps.tools is not None
    assert {"skills/list", "skills/get"} <= set(server._request_handlers)


def test_list_paginates_in_uri_order_and_keeps_the_server_origin_in_each_uri(fresh_mp):
    from threadkeeper.mcp_skills import ListSkillsParams, SKILL_LIST_PAGE_SIZE

    for index in range(SKILL_LIST_PAGE_SIZE + 1):
        _write_skill(fresh_mp, f"page-{index:03d}")

    first = _call_extension(fresh_mp, "skills/list", ListSkillsParams())
    second = _call_extension(
        fresh_mp, "skills/list", ListSkillsParams(cursor=first["nextCursor"]),
    )

    first_uris = [entry["uri"] for entry in first["skills"]]
    second_uris = [entry["uri"] for entry in second["skills"]]
    assert first["resultType"] == second["resultType"] == "complete"
    assert len(first_uris) == SKILL_LIST_PAGE_SIZE
    assert first_uris == sorted(first_uris)
    assert first_uris + second_uris == sorted(first_uris + second_uris)
    assert all(uri.startswith("skill://thread-keeper/") for uri in first_uris + second_uris)
    # The authority is an origin namespace, so a same-named skill from another
    # server cannot collapse into this server's entry.
    assert "skill://another-server/page-000/SKILL.md" not in first_uris


def test_get_returns_complete_manifest_and_read_bytes_match_each_digest(fresh_mp):
    from threadkeeper.mcp_skills import GetSkillParams, skill_catalog

    skill_dir = _write_skill(
        fresh_mp,
        "manifest-skill",
        extra_frontmatter="license: MIT\nmetadata:\n  owner: thread-keeper\n",
    )
    reference = skill_dir / "references" / "guide.txt"
    reference.parent.mkdir()
    reference.write_bytes(b"reference bytes\n")

    entry = next(item for item in skill_catalog() if item.name == "manifest-skill")
    result = _call_extension(
        fresh_mp, "skills/get", GetSkillParams(uri=entry.uri),
    )

    skill = result["skill"]
    assert result["resultType"] == "complete"
    assert skill["frontmatter"] == {
        "name": "manifest-skill",
        "description": "manifest-skill description",
        "license": "MIT",
        "metadata": {"owner": "thread-keeper"},
    }
    assert {item["uri"] for item in skill["resources"]} == {
        item.uri for item in entry.files
    }
    listed_resources = {
        str(resource.uri): resource
        for resource in asyncio.run(fresh_mp["mcp"].list_resources())
    }
    listed_uris = set(listed_resources)
    assert {item["uri"] for item in skill["resources"]} <= listed_uris
    for file_entry in skill["resources"]:
        assert listed_resources[file_entry["uri"]].size == file_entry["size"]
        contents = asyncio.run(fresh_mp["mcp"].read_resource(file_entry["uri"]))
        data = contents[0].content
        assert isinstance(data, bytes)
        assert len(data) == file_entry["size"]
        assert f"sha256:{hashlib.sha256(data).hexdigest()}" == file_entry["digest"]


def test_get_rejects_unknown_or_non_skill_markdown_uri(fresh_mp):
    from threadkeeper.mcp_skills import GetSkillParams

    _write_skill(fresh_mp, "known-skill")
    for uri in (
        "skill://thread-keeper/known-skill/references/guide.md",
        "skill://thread-keeper/missing/SKILL.md",
    ):
        with pytest.raises(MCPError) as raised:
            _call_extension(fresh_mp, "skills/get", GetSkillParams(uri=uri))
        assert raised.value.error.code == -32602


def test_resource_reads_reject_traversal_undeclared_and_oversized_files(fresh_mp):
    from threadkeeper.mcp_skills import skill_catalog

    skill_dir = _write_skill(fresh_mp, "safe-skill")
    (skill_dir / "private.txt").write_text("not declared", encoding="utf-8")
    bad_uris = (
        "skill://thread-keeper/safe-skill/../SKILL.md",
        "skill://thread-keeper/safe-skill/references/%2e%2e/SKILL.md",
        "skill://thread-keeper/safe-skill/private.txt",
    )
    for uri in bad_uris:
        with pytest.raises(ResourceNotFoundError):
            asyncio.run(fresh_mp["mcp"].read_resource(uri))

    large_dir = _write_skill(fresh_mp, "large-skill") / "references"
    large_dir.mkdir()
    (large_dir / "large.bin").write_bytes(b"x" * (1_048_576 + 1))
    assert "large-skill" not in {entry.name for entry in skill_catalog()}
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(
            fresh_mp["mcp"].read_resource(
                "skill://thread-keeper/large-skill/SKILL.md"
            )
        )


def test_skill_delivery_is_side_effect_free_and_filesystem_mirrors_remain_fallback(fresh_mp):
    from threadkeeper.tools.skills import _mirror_targets

    create = _tool(fresh_mp, "skill_manage")
    assert create(
        action="create",
        name="fallback-skill",
        description="Proves filesystem mirrors remain available.",
        content="# Fallback\n",
    ).startswith("ok path=")

    targets = _mirror_targets("fallback-skill")
    assert targets
    assert all((target / "SKILL.md").exists() for target in targets)

    conn = fresh_mp["db"].get_db()
    before = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
    uri = "skill://thread-keeper/fallback-skill/SKILL.md"
    assert asyncio.run(fresh_mp["mcp"].read_resource(uri))[0].content.startswith(b"---")
    after = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
    assert after == before
