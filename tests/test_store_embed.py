import io
import json

import pytest

from read.embed import DIMENSIONS, EMBED_MODEL, embed
from read.store import INDEX, INDEX_BODY, PIPELINE, ensure_index, is_local


class FakeBedrock:
    def __init__(self, dims=DIMENSIONS):
        self.dims, self.calls = dims, []

    def invoke_model(self, modelId, body):
        self.calls.append((modelId, json.loads(body)))
        return {"body": io.BytesIO(json.dumps({"embedding": [0.0] * self.dims}).encode())}


def test_embed_uses_titan_v2_1024_normalized():
    fake = FakeBedrock()
    assert len(embed("phonics", fake)) == DIMENSIONS
    model, body = fake.calls[0]
    assert model == EMBED_MODEL == "amazon.titan-embed-text-v2:0"
    assert body == {"inputText": "phonics", "dimensions": 1024, "normalize": True}


def test_embed_rejects_wrong_dimension():
    with pytest.raises(ValueError):
        embed("x", FakeBedrock(dims=256))


def test_is_local():
    assert is_local("http://localhost:9200")
    assert is_local("http://127.0.0.1:9200")
    assert not is_local("https://abc123.us-east-1.aoss.amazonaws.com")


def test_mapping_has_the_fields_retrieval_relies_on():
    props = INDEX_BODY["mappings"]["properties"]
    for f in ("version_id", "section_id", "text_sha256", "chunk_id", "work_id"):
        assert props[f]["type"] == "keyword"
    assert props["char_start"]["type"] == props["char_end"]["type"] == "integer"
    assert props["embedding"]["dimension"] == DIMENSIONS


class FakeIndices:
    def __init__(self, exists):
        self._exists, self.created = exists, []

    def exists(self, index):
        return self._exists

    def create(self, index, body):
        self.created.append(index)


class FakeTransport:
    def __init__(self):
        self.requests = []

    def perform_request(self, method, path, body=None):
        self.requests.append((method, path))


class FakeOS:
    def __init__(self, exists):
        self.indices, self.transport = FakeIndices(exists), FakeTransport()


def test_ensure_index_creates_once_and_always_puts_pipeline():
    new = FakeOS(exists=False)
    assert ensure_index(new) == [f"created index {INDEX}", f"put pipeline {PIPELINE}"]
    existing = FakeOS(exists=True)
    assert ensure_index(existing) == [f"put pipeline {PIPELINE}"]
    assert existing.indices.created == []
    assert existing.transport.requests == [("PUT", "/_search/pipeline/hybrid-norm")]
