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

PIPELINE_BODY = {
    "description": "Min-max normalize BM25 and k-NN scores, then weight them",
    "phase_results_processors": [
        {
            "normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {
                    "technique": "arithmetic_mean",
                    "parameters": {"weights": [KEYWORD_WEIGHT, SEMANTIC_WEIGHT]},
                },
            }
        }
    ],
}


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
