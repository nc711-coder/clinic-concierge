"""Embedder wiring tests. The real local model isn't downloaded here: fastembed is replaced by a stub."""
import sys
import types

import numpy as np
import pytest

from ingest.embed import HashEmbedder, get_embedder


@pytest.fixture
def fake_fastembed(monkeypatch):
    calls = {"passage": 0, "query": 0}

    class TextEmbedding:
        def __init__(self, model_name):
            self.model_name = model_name

        def passage_embed(self, texts):
            calls["passage"] += 1
            return (np.full(384, 0.1) for _ in texts)

        def query_embed(self, texts):
            calls["query"] += 1
            return (np.full(384, 0.2) for _ in texts)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding))
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")   # leftover OpenAI setting must be ignored
    monkeypatch.delenv("LOCAL_EMBEDDING_MODEL", raising=False)
    return calls


def test_local_embedder_is_default_choice_and_uses_query_mode(fake_fastembed):
    e = get_embedder("local")
    assert e.model == "BAAI/bge-small-en-v1.5" and e.dims == 384
    vecs = e.embed(["a", "b"])
    assert len(vecs) == 2 and isinstance(vecs[0], list) and len(vecs[0]) == 384
    q = e.embed_query("do you take kaiser?")
    assert q[0] == pytest.approx(0.2)            # questions go through query_embed, not passage_embed
    assert fake_fastembed["query"] == 1


def test_openai_without_key_explains_local_option(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="EMBEDDER=local"):
        get_embedder("openai")


def test_hash_embedder_unit_vectors():
    [v] = HashEmbedder(384).embed(["x"])
    assert len(v) == 384 and abs(sum(x * x for x in v) - 1) < 1e-9
