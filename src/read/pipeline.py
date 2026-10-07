"""Ingest and manage sources: used by scripts/ingest.py and the local dev server.

Steps for ingest_file: check metadata -> version ID -> extract -> write canonical text
(write-once) -> chunk (offsets verified) -> embed -> index passages -> write sections ->
verify counts -> license gate -> activate. A source is searchable only after activation.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from opensearchpy import helpers

from read.chunk import build_chunks
from read.embed import EMBED_MODEL, embed
from read.extract import Unsupported, extract
from read.ingest import check_meta, license_ok, local_version_id, now_iso, work_record
from read.service import Stores
from read.store import INDEX

TITAN_USD_PER_M_TOKENS = 0.02
EDITABLE = ("expires_on", "superseded_by")


class IngestError(Exception):
    pass


def ingest_file(
    data: bytes,
    filename: str,
    meta: dict,
    st: Stores,
    progress: Callable[[str], None] = print,
) -> dict:
    """Ingest one file; returns {"work_id", "version_id", "status", ...}. Raises IngestError."""
    problems = check_meta(meta)
    if problems:
        raise IngestError("missing metadata: " + "; ".join(problems))
    work_id = meta["work_id"]
    ver = local_version_id(data)
    old = st.works.get_item(Key={"work_id": work_id}).get("Item")
    if (
        old
        and old.get("source_version_id") == ver
        and old.get("status") in ("ready", "awaiting_license")
    ):
        if old["status"] == "ready" or not license_ok(meta):
            progress(f"unchanged ({ver}, {old['status']})")
            return {
                "work_id": work_id,
                "version_id": ver,
                "status": old["status"],
                "unchanged": True,
            }
        progress("file unchanged and license now verified: activating")
        return activate(work_id, meta, st, progress)

    progress("extracting text")
    try:
        ex = extract(data)
    except Unsupported as e:
        st.works.put_item(Item={**(old or {}), "work_id": work_id, "status": f"failed: {e}"})
        raise IngestError(f"unsupported format: {e}") from None
    canonical_key = st.text.put(work_id, ver, ex.text)
    sections, passages = build_chunks(work_id, ver, ex.text, ex.headings, ex.page_starts)
    words = sum(len(p["text"].split()) for p in passages)
    tokens = int(words * 1.3)
    progress(
        f"{ex.kind}, {len(ex.page_starts)} pages, {len(sections)} sections, "
        f"{len(passages)} passages; embedding about {tokens:,} tokens "
        f"(~${tokens / 1e6 * TITAN_USD_PER_M_TOKENS:.4f})"
    )
    row = work_record(
        meta,
        ver,
        filename,
        canonical_key,
        len(passages),
        "ingesting",
        old.get("active_version_id") if old else None,
        old.get("activated_at") if old else None,
    )
    st.works.put_item(Item=row)

    done = [0]

    def emb(p: dict) -> list[float]:
        v = embed(p["text"], st.bedrock)
        done[0] += 1
        if done[0] % 100 == 0:
            progress(f"embedded {done[0]}/{len(passages)}")
        return v

    with ThreadPoolExecutor(max_workers=8) as pool:
        vectors = list(pool.map(emb, passages))
    progress("indexing passages")
    actions = [
        {
            "_index": INDEX,
            "_id": p["chunk_id"],
            "_source": {**p, "title": meta["title"], "embedding": v, "embed_model": EMBED_MODEL},
        }
        for p, v in zip(passages, vectors, strict=True)
    ]
    _, errors = helpers.bulk(st.os, actions, raise_on_error=False, refresh=True)
    if errors:
        st.works.put_item(Item={**row, "status": "failed: indexing"})
        raise IngestError(f"{len(errors)} passages failed to index")
    with st.sections.batch_writer() as batch:
        for s in sections:
            batch.put_item(Item={k: v for k, v in s.items() if v is not None})
    if not license_ok(meta):
        _verify_counts(st, ver, len(passages), row)
        st.works.put_item(Item={**row, "status": "awaiting_license"})
        progress("stored but NOT searchable: awaiting license sign-off")
        return {"work_id": work_id, "version_id": ver, "status": "awaiting_license"}
    st.works.put_item(Item={**row, "status": "awaiting_activation"})
    return activate(work_id, meta, st, progress)


def _verify_counts(st: Stores, ver: str, expected: int, row: dict) -> None:
    n = st.os.count(index=INDEX, body={"query": {"term": {"version_id": ver}}})["count"]
    if n != expected:
        st.works.put_item(Item={**row, "status": "failed: verification"})
        raise IngestError(f"index has {n} passages for {ver}, expected {expected}")


def activate(work_id: str, meta: dict, st: Stores, progress: Callable[[str], None] = print) -> dict:
    """Make the stored version searchable: verify, check the license gate, switch, retire old."""
    row = st.works.get_item(Key={"work_id": work_id}).get("Item")
    if not row:
        raise IngestError(f"no such source: {work_id}")
    if not license_ok(meta):
        raise IngestError("license not verified: license, license_verified_by, license_verified_on")
    ver = row["source_version_id"]
    _verify_counts(st, ver, int(row["passage_count"]), row)
    previous = row.get("active_version_id")
    tags = work_record(
        meta, ver, row["source_key"], row["canonical_key"], int(row["passage_count"]), "ready"
    )
    st.works.put_item(Item={**tags, "active_version_id": ver, "activated_at": now_iso()})
    if previous and previous != ver:  # retire the old version only after the new one is live
        _delete_version(st, previous)
        progress(f"retired previous version {previous}")
    progress(f"ready and searchable ({ver})")
    return {"work_id": work_id, "version_id": ver, "status": "ready"}


def _delete_version(st: Stores, ver: str) -> None:
    st.os.delete_by_query(index=INDEX, body={"query": {"term": {"version_id": ver}}}, refresh=True)
    stale, kwargs = (
        [],
        {"FilterExpression": "version_id = :v", "ExpressionAttributeValues": {":v": ver}},
    )
    while True:
        page = st.sections.scan(**kwargs)
        stale += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with st.sections.batch_writer() as batch:
        for s in stale:
            batch.delete_item(Key={"section_id": s["section_id"]})


def set_status(work_id: str, status: str, st: Stores) -> None:
    """Deactivate ("inactive") or restore ("ready") a source without touching its data."""
    row = st.works.get_item(Key={"work_id": work_id}).get("Item")
    if not row:
        raise IngestError(f"no such source: {work_id}")
    if status == "ready" and not license_ok(row):
        raise IngestError("can't reactivate: license not verified")
    if status == "ready" and row.get("source_version_id") != row.get("active_version_id"):
        raise IngestError("can't reactivate: stored version was never activated")
    st.works.put_item(Item={**row, "status": status})


def update_tags(work_id: str, changes: dict, st: Stores) -> dict:
    """Edit expires_on / superseded_by on the works row; returns the new row."""
    row = st.works.get_item(Key={"work_id": work_id}).get("Item")
    if not row:
        raise IngestError(f"no such source: {work_id}")
    for k, v in changes.items():
        if k not in EDITABLE:
            raise IngestError(f"{k} can't be edited here")
        if v in (None, ""):
            row.pop(k, None)
        else:
            row[k] = v
    problems = [p for p in check_meta({**row}) if "expires_on" in p]
    if problems:
        raise IngestError("; ".join(problems))
    st.works.put_item(Item=row)
    return row


def delete_work(work_id: str, st: Stores) -> None:
    """Remove a source from search and the tables. Canonical text files are kept (write-once)."""
    row = st.works.get_item(Key={"work_id": work_id}).get("Item")
    if not row:
        raise IngestError(f"no such source: {work_id}")
    st.os.delete_by_query(index=INDEX, body={"query": {"term": {"work_id": work_id}}}, refresh=True)
    for ver in {row.get("source_version_id"), row.get("active_version_id")} - {None}:
        _delete_version(st, ver)
    st.works.delete_item(Key={"work_id": work_id})
