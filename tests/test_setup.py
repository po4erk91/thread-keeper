from __future__ import annotations


def test_install_mcp_servers_pins_imports_to_configured_package(
    tmp_path, monkeypatch,
):
    import threadkeeper._setup as setup_mod
    import threadkeeper.adapters as adapters_mod

    captured = {}

    class FakeAdapter:
        name = "fake"

        def register_mcp_server(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    monkeypatch.setattr(setup_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        adapters_mod, "installed_adapters", lambda: [FakeAdapter()],
    )

    assert setup_mod.install_mcp_servers(dry_run=False) == [
        "mcp_server[fake]: ok"
    ]
    assert captured["env"] == {
        "PYTHONPATH": str(tmp_path),
        "PYTHONSAFEPATH": "1",
    }

