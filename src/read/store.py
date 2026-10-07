"""OpenSearch index definition and client.

Development runs against a local OpenSearch in Docker (docker-compose.yml, free); the AWS
OpenSearch Serverless collection is used only for short, approved tests and demos
(docs/decisions.md). Both take the same index mapping and hybrid-norm pipeline.
"""

import os
from urllib.parse import urlparse

from read.embed import DIMENSIONS

INDEX = "passages"
PIPELINE = "hybrid-norm"
KEYWORD_WEIGHT, SEMANTIC_WEIGHT = 0.3, 0.7

INDEX_BODY = {
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "work_id": {"type": "keyword"},
            "version_id": {"type": "keyword"},
            "section_id": {"type": "keyword"},
            "char_start": {"type": "integer"},
            "char_end": {"type": "integer"},
            "text": {"type": "text"},
            "text_sha256": {"type": "keyword"},
            "title": {"type": "text"},
            "section_path": {"type": "keyword"},
            "page": {"type": "integer"},
            "page_end": {"type": "integer"},
            "embed_model": {"type": "keyword"},
            "embedding": {
                "type": "knn_vector",
                "dimension": DIMENSIONS,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"},
            },
        }
    },
}


def pipeline_body(keyword_weight: float = KEYWORD_WEIGHT) -> dict:
    """Min-max normalize BM25 and k-NN scores, then combine with these weights."""
    kw = round(min(max(keyword_weight, 0.0), 1.0), 3)
    return {
        "description": "Min-max normalize BM25 and k-NN scores, then weight them",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [kw, round(1 - kw, 3)]},
                    },
                }
            }
        ],
    }


PIPELINE_BODY = pipeline_body()


def is_local(endpoint: str) -> bool:
    return urlparse(endpoint).hostname in ("localhost", "127.0.0.1")


def opensearch_client(endpoint: str | None = None, session=None):
    """Client for OPENSEARCH_ENDPOINT: plain HTTP for local Docker, SigV4 for Serverless."""
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    endpoint = endpoint or os.environ["OPENSEARCH_ENDPOINT"]
    url = urlparse(endpoint)
    if is_local(endpoint):
        return OpenSearch(hosts=[endpoint], use_ssl=url.scheme == "https", timeout=30)
    if session is None:
        import boto3

        session = boto3.Session()
    auth = AWSV4SignerAuth(session.get_credentials(), session.region_name, "aoss")
    return OpenSearch(
        hosts=[{"host": url.hostname, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )


def ensure_index(client) -> list[str]:
    """Create the passages index and hybrid-norm pipeline if missing; return what was done."""
    done = []
    if not client.indices.exists(index=INDEX):
        client.indices.create(index=INDEX, body=INDEX_BODY)
        done.append(f"created index {INDEX}")
    client.transport.perform_request("PUT", f"/_search/pipeline/{PIPELINE}", body=PIPELINE_BODY)
    done.append(f"put pipeline {PIPELINE}")
    return done


# ---- DynamoDB: works and sections tables (DynamoDB Local in development) ----

WORKS_TABLE = os.environ.get("WORKS_TABLE", "read-poc-works")
SECTIONS_TABLE = os.environ.get("SECTIONS_TABLE", "read-poc-sections")
TABLE_KEYS = {WORKS_TABLE: "work_id", SECTIONS_TABLE: "section_id"}


def dynamodb_resource(endpoint: str | None = None):
    """DynamoDB resource; a localhost endpoint means DynamoDB Local with dummy credentials,
    so a local run can never touch the real AWS account."""
    import boto3

    endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")
    if endpoint and is_local(endpoint):
        return boto3.resource(
            "dynamodb",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="local",
            aws_secret_access_key="local",
        )
    return boto3.resource("dynamodb")


def ensure_tables(ddb) -> list[str]:
    """Create the works and sections tables (on-demand billing) if missing."""
    existing = {t.name for t in ddb.tables.all()}
    done = []
    for name, key in TABLE_KEYS.items():
        if name in existing:
            continue
        table = ddb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        done.append(f"created table {name}")
    return done


# ---- Canonical text: write-once files (the works-text S3 bucket in AWS) ----


class WriteOnceError(Exception):
    """A canonical text file already exists with different content."""


class LocalTextStore:
    """works-text/<work_id>/<version_id>.txt on local disk, write-once like the S3 bucket."""

    def __init__(self, root: str | os.PathLike):
        from pathlib import Path

        self.root = Path(root)

    def key(self, work_id: str, version_id: str) -> str:
        return f"{work_id}/{version_id}.txt"

    def put(self, work_id: str, version_id: str, text: str) -> str:
        path = self.root / self.key(work_id, version_id)
        data = text.encode("utf-8")
        if path.exists():
            if path.read_bytes() != data:
                raise WriteOnceError(f"{path} exists with different content")
            return self.key(work_id, version_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.key(work_id, version_id)

    def get(self, work_id: str, version_id: str) -> str:
        return (self.root / self.key(work_id, version_id)).read_bytes().decode("utf-8")


# ---- Temporary material store: plans-temp (S3 with a 24-hour lifecycle delete in AWS) ----


class TempStore:
    """Submitted material and its review, kept at most `hours` and never indexed (rule 7)."""

    def __init__(self, root: str | os.PathLike, hours: float = 24):
        from pathlib import Path

        self.root, self.hours = Path(root), hours

    def new(self) -> str:
        import uuid

        rid = uuid.uuid4().hex[:16]
        (self.root / rid).mkdir(parents=True)
        return rid

    def path(self, rid: str, name: str):
        if not rid.isalnum() or not self.root.joinpath(rid).is_dir():
            raise KeyError(rid)
        return self.root / rid / name

    def put_json(self, rid: str, name: str, value) -> None:
        import json

        self.path(rid, name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def get_json(self, rid: str, name: str):
        import json

        return json.loads(self.path(rid, name).read_text(encoding="utf-8"))

    def purge(self, now: float | None = None) -> int:
        """Delete every review folder older than `hours`; returns how many were removed."""
        import shutil
        import time

        if not self.root.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - self.hours * 3600
        gone = 0
        for d in self.root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                gone += 1
        return gone
