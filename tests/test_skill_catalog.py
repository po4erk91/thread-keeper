"""Full skill catalog for loop writers and duplicate detection by meaning."""
from __future__ import annotations

import numpy as np

from tests.test_skills import _bootstrap_skills


def _skill(root, name, description):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody\n", encoding="utf-8",
    )


def _fake_vectors(texts):
    """Same leading word → same direction; enough to rank by meaning."""
    vectors = []
    for text in texts:
        vector = np.zeros(8, dtype="float32")
        vector[sum(map(ord, text.split(":")[0].split()[0])) % 7] = 1.0
        vector[7] += 0.1
        vectors.append(vector / np.linalg.norm(vector))
    return vectors


def test_catalog_lists_every_local_and_plugin_skill(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper import skill_catalog
    import threadkeeper.skill_audit as audit

    root = pkg["skills_root"]
    for index in range(20):
        _skill(root, f"skill-{index:02d}", f"Use when task {index} happens.")
    plugin = tmp_path / "plugin" / "brainstorming"
    _skill(plugin.parent, "brainstorming", "Use when starting creative work.")
    monkeypatch.setattr(audit, "_external_skill_index", lambda: {"brainstorming": [plugin / "SKILL.md"]})
    block = skill_catalog.catalog_prompt_block(pkg["db"].get_db())
    assert "all 21 installed skills (20 local, 1 plugin)" in block
    assert "skill-19" in block
    assert "brainstorming [PLUGIN]" in block


def test_semantic_neighbors_and_pairs(tmp_path, monkeypatch):
    _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper import skill_catalog

    monkeypatch.setattr(skill_catalog, "_vectors", _fake_vectors)
    entries = [
        skill_catalog.CatalogEntry("review-code", "Use when reviewing.", "local", False, 0),
        skill_catalog.CatalogEntry("review-pr", "Use when reviewing a PR.", "local", False, 0),
        skill_catalog.CatalogEntry("deploy-app", "Use when deploying.", "local", False, 0),
    ]
    neighbors = skill_catalog.semantic_neighbors(entries, "review-docs", "Use when reviewing docs.")
    assert {name for name, _ in neighbors[:2]} == {"review-code", "review-pr"}
    pairs = skill_catalog.duplicate_pairs(entries, 0.9)
    assert [(p["left"], p["right"]) for p in pairs] == [("review-code", "review-pr")]


def test_no_embeddings_means_no_duplicate_verdict(tmp_path, monkeypatch):
    _bootstrap_skills(tmp_path, monkeypatch)
    from threadkeeper import skill_catalog

    monkeypatch.setattr(skill_catalog, "_vectors", lambda texts: None)
    entries = [skill_catalog.CatalogEntry("a", "x", "local", False, 0)]
    assert skill_catalog.semantic_neighbors(entries, "b", "y") is None
    assert skill_catalog.duplicate_pairs(entries * 2, 0.5) is None
