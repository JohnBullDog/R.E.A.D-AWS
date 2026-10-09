"""The S3 passage index (D95) against an in-memory fake S3."""

import io

import numpy as np
import pytest

from read.embed import DIMENSIONS
from read.retrieve import fuse
from read.s3index import EMPTY, S3Index, Shard, tokens


class NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


def vec(*hot):
    v = np.zeros(DIMENSIONS, dtype=np.float32)
    for i in hot:
        v[i] = 1.0
    return (v / np.linalg.norm(v)).tolist()


def row(work, ver, i, text, *hot):
    return {
        "chunk_id": f"{work}:{ver}:p{i:05d}",
        "work_id": work,
        "version_id": ver,
        "section_id": f"{work}:{ver}:s0000",
        "char_start": i * 10,
        "char_end": i * 10 + 5,
        "text": text,
        "text_sha256": "h",
        "title": "T",
        "page": 1,
        "embedding": vec(*hot),
    }


@pytest.fixture
def idx():
    ix = S3Index(FakeS3(), "app")
    ix.upsert(
        [
            row("wwc", "v1", 0, "Teach blending of sounds in CVC words", 0),
            row("wwc", "v1", 1, "Phonemic awareness activities daily", 1),
            row("nrp", "v2", 0, "Fluency practice with repeated reading", 2),
        ]
    )
    return ix


def test_tokens():
    assert tokens("Blend CVC-words, daily!") == ["blend", "cvc", "words", "daily"]


def test_shard_round_trip_keeps_passages_and_vectors(idx):
    data = idx.s3.objects["index/wwc/v1.json.gz"]
    shard = Shard.load(data)
    assert [p["chunk_id"] for p in shard.passages] == ["wwc:v1:p00000", "wwc:v1:p00001"]
    assert shard.vectors.shape == (2, DIMENSIONS) and shard.vectors[1, 1] == 1.0
    assert "embedding" not in shard.passages[0]
    assert Shard.load(EMPTY.dump()).passages == []


def test_upsert_replaces_by_chunk_id_and_counts(idx):
    assert idx.count("v1") == 2 and idx.count("v2") == 1 and idx.count("nope") == 0
    idx.upsert([row("wwc", "v1", 1, "Replaced text", 1)])
    assert idx.count("v1") == 2
    assert idx.keyword("replaced", ["v1"], 5)[0][0]["chunk_id"] == "wwc:v1:p00001"


def test_searches_only_see_active_versions(idx):
    hits = idx.semantic(vec(2), ["v1", "v2"], 5)
    assert hits[0][0]["chunk_id"] == "nrp:v2:p00000" and hits[0][1] == pytest.approx(1.0)
    assert all(p["version_id"] == "v1" for p, _ in idx.semantic(vec(2), ["v1"], 5))
    assert idx.semantic(vec(0), [], 5) == [] and idx.keyword("blending", [], 5) == []


def test_bm25_matches_any_word_and_ranks_rarer_terms_higher(idx):
    hits = idx.keyword("blending fluency zebra", ["v1", "v2"], 5)
    assert {p["chunk_id"] for p, _ in hits} == {"wwc:v1:p00000", "nrp:v2:p00000"}
    assert idx.keyword("zebra", ["v1", "v2"], 5) == []
    assert idx.keyword("?!", ["v1"], 5) == []


def test_hybrid_is_fuse_of_both_lists(idx):
    q = vec(0)
    expected = fuse(idx.keyword("blending", ["v1"], 5), idx.semantic(q, ["v1"], 5), 0.3, 5)
    assert idx.hybrid("blending", q, ["v1"], 5, 0.3) == expected
    assert expected[0][0]["chunk_id"] == "wwc:v1:p00000"


def test_delete_writes_empty_shards_and_a_fresh_reader_sees_them(idx):
    assert idx.delete_version("v1") == 2
    assert idx.count("v1") == 0 and idx.semantic(vec(0), ["v1"], 5) == []
    assert idx.delete_version("never") == 0
    assert idx.delete_work("nrp") == 1
    other = S3Index(idx.s3, "app")  # another Lambda
    assert other.count("v2") == 0 and other.keyword("fluency", ["v2"], 5) == []


def test_a_new_version_written_elsewhere_is_found():
    s3 = FakeS3()
    api, worker = S3Index(s3, "app"), S3Index(s3, "app")
    assert api.semantic(vec(0), ["v9"], 5) == []  # not there yet
    worker.upsert([row("w", "v9", 0, "new source", 0)])
    assert api.semantic(vec(0), ["v9"], 5)[0][0]["chunk_id"] == "w:v9:p00000"


def test_slashes_rejected():
    with pytest.raises(ValueError):
        S3Index(FakeS3(), "app").upsert([row("a/b", "v1", 0, "x", 0)])
