"""Embedding loaders must resolve immutable Hub snapshots before loading."""
from __future__ import annotations

import importlib
import sys
from types import ModuleType


def _reimport(monkeypatch, tmp_path, **env):
    base = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
    }
    base.update(env)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    for name in [name for name in sys.modules if name.startswith("threadkeeper")]:
        del sys.modules[name]
    return importlib.import_module("threadkeeper.embeddings")


def test_onnx_loader_uses_configured_pinned_snapshot(monkeypatch, tmp_path):
    emb = _reimport(
        monkeypatch, tmp_path,
        THREADKEEPER_EMBED_REVISION="test-pinned-revision",
        THREADKEEPER_EMBED_LOCAL_FILES_ONLY="1",
    )
    seen = {}

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            seen["loader"] = kwargs

        @staticmethod
        def list_supported_models():
            return [{
                "model": emb.FASTEMBED_MODEL_ID,
                "sources": {"hf": "Qdrant/pinned-onnx-artifact"},
            }]

    fake_fastembed = ModuleType("fastembed")
    fake_fastembed.TextEmbedding = FakeTextEmbedding
    fake_hub = ModuleType("huggingface_hub")

    def snapshot_download(**kwargs):
        seen["snapshot"] = kwargs
        return "/cached/pinned-snapshot"

    fake_hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setattr(emb, "SEMANTIC_AVAILABLE", True)

    assert isinstance(emb._get_model(), FakeTextEmbedding)
    assert seen["snapshot"]["repo_id"] == "Qdrant/pinned-onnx-artifact"
    assert seen["snapshot"]["revision"] == "test-pinned-revision"
    assert seen["snapshot"]["cache_dir"] == str(emb.EMBED_CACHE_DIR)
    assert seen["snapshot"]["local_files_only"] is True
    assert seen["loader"] == {
        "model_name": emb.FASTEMBED_MODEL_ID,
        "specific_model_path": "/cached/pinned-snapshot",
    }


def test_sentence_transformer_loader_passes_configured_revision(monkeypatch, tmp_path):
    emb = _reimport(
        monkeypatch, tmp_path,
        THREADKEEPER_EMBED_REVISION="test-pinned-revision",
    )
    seen = {}

    class FakeSentenceTransformer:
        def __init__(self, model_name, **kwargs):
            seen["model_name"] = model_name
            seen["kwargs"] = kwargs

    fake_st = ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setattr(emb, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(emb, "EMBED_BACKEND", "sentence-transformers")

    assert isinstance(emb._get_model(), FakeSentenceTransformer)
    assert seen == {
        "model_name": emb.EMBED_MODEL_NAME,
        "kwargs": {
            "cache_folder": str(emb.EMBED_CACHE_DIR),
            "revision": "test-pinned-revision",
        },
    }
