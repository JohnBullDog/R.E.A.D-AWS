"""Ingest one source file with its meta.json sidecar.

    python scripts/ingest.py corpus/<file> corpus/<file>.meta.json --local

--local uses the Docker services (OpenSearch at :9200, DynamoDB Local at :8000) and writes
canonical text to data/works-text/. Embeddings call Bedrock Titan with the read-poc profile
(about $0.02 per million tokens; the estimate is printed first).

Steps: check metadata -> version ID -> extract -> write canonical text (write-once) -> chunk
(offsets verified) -> embed -> index passages -> write sections -> verify counts -> license gate
-> activate (or leave as awaiting_license). A source is searchable only after activation.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from opensearchpy import helpers  # noqa: E402

from read.chunk import build_chunks  # noqa: E402
from read.embed import EMBED_MODEL, embed  # noqa: E402
from read.extract import Unsupported, extract  # noqa: E402
from read.ingest import check_meta, license_ok, local_version_id, now_iso, work_record  # noqa: E402
from read.store import (  # noqa: E402
    INDEX,
    SECTIONS_TABLE,
    WORKS_TABLE,
    LocalTextStore,
    dynamodb_resource,
    ensure_index,
    ensure_tables,
    opensearch_client,
)

LOCAL_OS = "http://localhost:9200"
LOCAL_DDB = "http://localhost:8000"
TITAN_USD_PER_M_TOKENS = 0.02


def fail(msg: str) -> None:
    sys.exit(f"failed: {msg}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("meta")
    ap.add_argument("--local", action="store_true", help="use the local Docker services")
    ap.add_argument("--profile", default="read-poc", help="AWS profile for Bedrock embeddings")
    args = ap.parse_args()
    if not args.local:
        fail("only --local is supported until the AWS stack exists")

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    problems = check_meta(meta)
    if problems:
        fail("missing metadata: " + "; ".join(problems))
    work_id = meta["work_id"]
    data = Path(args.file).read_bytes()
    ver = local_version_id(data)

    ddb = dynamodb_resource(LOCAL_DDB)
    ensure_tables(ddb)
    works, sections_tbl = ddb.Table(WORKS_TABLE), ddb.Table(SECTIONS_TABLE)
    old = works.get_item(Key={"work_id": work_id}).get("Item")
    if (
        old
        and old.get("source_version_id") == ver
        and old.get("status") in ("ready", "awaiting_license")
    ):
        if old["status"] == "ready" or not license_ok(meta):
            print(f"{work_id}: unchanged ({ver}, {old['status']})")
            return

    try:
        ex = extract(data)
    except Unsupported as e:
        works.put_item(Item={**(old or {}), "work_id": work_id, "status": f"failed: {e}"})
        fail(f"unsupported format: {e}")

    store = LocalTextStore(ROOT / "data" / "works-text")
    canonical_key = store.put(work_id, ver, ex.text)
    sections, passages = build_chunks(work_id, ver, ex.text, ex.headings, ex.page_starts)
    words = sum(len(p["text"].split()) for p in passages)
    print(
        f"{work_id}: {ex.kind}, {len(ex.page_starts)} pages, {len(sections)} sections, "
        f"{len(passages)} passages; embedding about {int(words * 1.3):,} tokens "
        f"(~${words * 1.3 / 1e6 * TITAN_USD_PER_M_TOKENS:.4f})"
    )
    status_row = work_record(
        meta,
        ver,
        Path(args.file).name,
        canonical_key,
        len(passages),
        "ingesting",
        old.get("active_version_id") if old else None,
        old.get("activated_at") if old else None,
    )
    works.put_item(Item=status_row)

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    br = session.client(
        "bedrock-runtime", config=Config(retries={"max_attempts": 8, "mode": "adaptive"})
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        vectors = list(pool.map(lambda p: embed(p["text"], br), passages))

    os_client = opensearch_client(LOCAL_OS)
    ensure_index(os_client)
    title = meta["title"]
    actions = [
        {
            "_index": INDEX,
            "_id": p["chunk_id"],
            "_source": {**p, "title": title, "embedding": v, "embed_model": EMBED_MODEL},
        }
        for p, v in zip(passages, vectors, strict=True)
    ]
    ok, errors = helpers.bulk(os_client, actions, raise_on_error=False, refresh=True)
    if errors:
        works.put_item(Item={**status_row, "status": "failed: indexing"})
        fail(f"{len(errors)} passages failed to index")
    with sections_tbl.batch_writer() as batch:
        for s in sections:
            batch.put_item(Item={k: v for k, v in s.items() if v is not None})

    indexed = os_client.count(index=INDEX, body={"query": {"term": {"version_id": ver}}})["count"]
    if indexed != len(passages):
        works.put_item(Item={**status_row, "status": "failed: verification"})
        fail(f"index has {indexed} passages for {ver}, expected {len(passages)}")

    if not license_ok(meta):
        works.put_item(Item={**status_row, "status": "awaiting_license"})
        print(
            f"{work_id}: stored but NOT searchable (awaiting_license). A person must fill in "
            "license, license_verified_by and license_verified_on in the meta.json, then re-run."
        )
        return

    previous = old.get("active_version_id") if old else None
    works.put_item(
        Item={**status_row, "status": "ready", "active_version_id": ver, "activated_at": now_iso()}
    )
    if previous and previous != ver:  # retire the old version only after the new one is live
        os_client.delete_by_query(
            index=INDEX, body={"query": {"term": {"version_id": previous}}}, refresh=True
        )
        stale = sections_tbl.scan(
            FilterExpression="version_id = :v", ExpressionAttributeValues={":v": previous}
        )["Items"]
        with sections_tbl.batch_writer() as batch:
            for s in stale:
                batch.delete_item(Key={"section_id": s["section_id"]})
        print(f"{work_id}: retired previous version {previous}")
    print(f"{work_id}: ready and searchable ({ver})")


if __name__ == "__main__":
    main()
