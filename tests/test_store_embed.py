import io
import json

import pytest

from read.embed import DIMENSIONS, EMBED_MODEL, embed
from read.pgindex import FIELDS, SCHEMA, AuroraIndex, or_query, param
from read.store import S3FileStore, S3TempStore, S3TextStore, WriteOnceError


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


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeRdsData:
    """Records statements; answers SELECTs with canned JSON records."""

    def __init__(self, records=(), resume_failures=0):
        self.records, self.resume_failures, self.calls = list(records), resume_failures, []

    def execute_statement(self, **kw):
        if self.resume_failures:
            self.resume_failures -= 1
            raise ClientError("DatabaseResumingException")
        self.calls.append(kw)
        return {"formattedRecords": json.dumps(self.records), "numberOfRecordsUpdated": 2}

    def batch_execute_statement(self, **kw):
        self.calls.append(kw)
        return {}


def index(fake):
    return AuroraIndex(fake, "cluster-arn", "secret-arn")


def test_or_query_matches_any_word_and_drops_punctuation():
    assert or_query("Blending: CVC words & blending!") == "blending | cvc | words"
    assert or_query("'; DROP TABLE --") == "drop | table"
    assert or_query("?!") == ""


def test_param_types():
    assert param("n", 3)["value"] == {"longValue": 3}
    assert param("x", None)["value"] == {"isNull": True}
    assert param("s", "v1")["value"] == {"stringValue": "v1"}


def test_schema_sizes_vector_to_embedding_dimension():
    assert any(f"vector({DIMENSIONS})" in sql for sql in SCHEMA)


def test_upsert_batches_rows_and_casts_ints():
    fake = FakeRdsData()
    row = {f: "x" for f in FIELDS} | {"char_start": "4", "char_end": 9, "page": None, "page_end": 2}
    assert (
        index(fake).upsert([{**row, "chunk_id": str(i), "embedding": [0.5] * 3} for i in range(23)])
        == 23
    )
    assert [len(c["parameterSets"]) for c in fake.calls] == [10, 10, 3]
    first = {p["name"]: p["value"] for p in fake.calls[0]["parameterSets"][0]}
    assert first["char_start"] == {"longValue": 4}
    assert first["page"] == {"isNull": True}
    assert first["embedding"] == {"stringValue": "[0.5,0.5,0.5]"}
    assert "ON CONFLICT (chunk_id)" in fake.calls[0]["sql"]
    assert fake.calls[0]["resourceArn"] == "cluster-arn" and fake.calls[0]["database"] == "read"


def test_searches_filter_to_active_versions_and_skip_when_none():
    fake = FakeRdsData([{**{f: "x" for f in FIELDS}, "chunk_id": "a", "score": 0.4}])
    hits = index(fake).semantic([0.1] * 3, ["v1", "v2"], 5)
    assert hits[0][0]["chunk_id"] == "a" and hits[0][1] == 0.4
    params = {p["name"]: p["value"] for p in fake.calls[0]["parameters"]}
    assert params["v"] == {"stringValue": "v1,v2"} and params["n"] == {"longValue": 5}
    assert "version_id = ANY" in fake.calls[0]["sql"]
    assert "embedding" not in hits[0][0]
    assert index(fake).keyword("phonics", [], 5) == []
    assert index(fake).keyword("?!", ["v1"], 5) == []
    assert len(fake.calls) == 1


def test_retries_while_the_cluster_resumes(monkeypatch):
    monkeypatch.setattr("read.pgindex.time.sleep", lambda s: None)
    fake = FakeRdsData([{"n": 7}], resume_failures=2)
    assert index(fake).count("v1") == 7


def test_other_errors_are_not_retried():
    class Broken(FakeRdsData):
        def execute_statement(self, **kw):
            raise ClientError("BadRequestException")

    with pytest.raises(ClientError):
        index(Broken()).count("v1")


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {}


def test_s3_text_store_is_write_once():
    st = S3TextStore(FakeS3(), "text")
    assert st.put("w", "v1", "café") == "w/v1.txt"
    assert st.put("w", "v1", "café") == "w/v1.txt"  # same content: fine
    assert st.get("w", "v1") == "café"
    with pytest.raises(WriteOnceError):
        st.put("w", "v1", "changed")


def test_s3_temp_store_round_trip_and_rejects_bad_keys():
    st = S3TempStore(FakeS3(), "temp")
    rid = st.new()
    assert st.exists(rid, ".created") and not st.exists(rid, "review.json")
    st.put_json(rid, "goal.json", {"grade_band": "K-2"})
    assert st.get_json(rid, "goal.json") == {"grade_band": "K-2"}
    with pytest.raises(FileNotFoundError):
        st.get_json(rid, "review.json")
    for bad in ("../x", "a/b"):
        with pytest.raises(KeyError):
            st.put_json(bad, "goal.json", {})
    with pytest.raises(KeyError):
        st.put_json(rid, "x/y.json", {})


def test_s3_file_store_missing_is_none():
    st = S3FileStore(FakeS3(), "app")
    assert st.get("checklist.json") is None and st.get_json("checklist.json") is None
    st.put_json("checklist.json", {"questions": []})
    assert st.get_json("checklist.json") == {"questions": []}
