"""Passage index as files in S3, searched in memory: vectors with numpy, keywords with BM25 (D95).

Chosen over Aurora because the account's Free plan only allows Aurora "express" clusters,
which CloudFormation can't create. One shard per source version:
  index/<work_id>/<version_id>.json.gz   {"passages": [...], "vectors": base64 float32}
Each ingest writes only its own shard, so two uploads never overwrite each other. A deleted
version is overwritten with an empty shard (the roles can't delete objects). Shards are cached
in memory for the life of the Lambda; a version's contents never change once written.

Same interface as read.pgindex.AuroraIndex, so the rest of the code doesn't care which is used.
"""

import base64
import gzip
import json
import math
import re
from collections import Counter

import numpy as np

from read.embed import DIMENSIONS

PREFIX = "index/"
K1, B = 1.2, 0.75  # BM25 defaults (OpenSearch's too)
TOKEN = re.compile(r"[a-z0-9]+")
FIELDS = (
    "chunk_id",
    "work_id",
    "version_id",
    "section_id",
    "char_start",
    "char_end",
    "text",
    "text_sha256",
    "title",
    "section_path",
    "page",
    "page_end",
    "embed_model",
)


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def _missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")


class Shard:
    """One version's passages, vectors, and term counts, ready to search."""

    def __init__(self, passages: list[dict], vectors: np.ndarray):
        self.passages, self.vectors = passages, vectors
        self.tf = [Counter(tokens(p["text"])) for p in passages]
        self.lengths = [sum(c.values()) for c in self.tf]

    @classmethod
    def load(cls, data: bytes) -> "Shard":
        doc = json.loads(gzip.decompress(data))
        raw = base64.b64decode(doc.get("vectors", ""))
        vectors = np.frombuffer(raw, dtype=np.float32).reshape(-1, DIMENSIONS)
        return cls(doc["passages"], vectors)

    def dump(self) -> bytes:
        vec = np.ascontiguousarray(self.vectors, dtype=np.float32).tobytes()
        doc = {"passages": self.passages, "vectors": base64.b64encode(vec).decode("ascii")}
        return gzip.compress(json.dumps(doc, ensure_ascii=False).encode("utf-8"))


EMPTY = Shard([], np.zeros((0, DIMENSIONS), dtype=np.float32))


class S3Index:
    """The passages index. s3 is a boto3 S3 client; bucket holds index/ shards."""

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket
        self.cache: dict[str, Shard] = {}  # version_id -> shard
        self.keys: dict[str, str] = {}  # version_id -> object key

    # ---------- storage ----------

    def _key(self, work_id: str, version_id: str) -> str:
        if "/" in work_id or "/" in version_id:
            raise ValueError("work_id and version_id can't contain '/'")
        return f"{PREFIX}{work_id}/{version_id}.json.gz"

    def _list(self, prefix: str = PREFIX) -> list[str]:
        keys, kw = [], {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            page = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return keys
            kw["ContinuationToken"] = page["NextContinuationToken"]

    def _locate(self, version_id: str) -> str | None:
        if version_id not in self.keys:
            for key in self._list():
                self.keys[key.rsplit("/", 1)[1].removesuffix(".json.gz")] = key
        return self.keys.get(version_id)

    def _get(self, version_id: str, fresh: bool = False) -> Shard:
        if fresh or version_id not in self.cache:
            key = self._locate(version_id)
            if key is None:
                return EMPTY
            try:
                data = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception as exc:
                if not _missing(exc):
                    raise
                return EMPTY
            self.cache[version_id] = Shard.load(data)
        return self.cache[version_id]

    def _put(self, work_id: str, version_id: str, shard: Shard) -> None:
        key = self._key(work_id, version_id)
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=shard.dump(), ContentType="application/gzip"
        )
        self.keys[version_id], self.cache[version_id] = key, shard

    # ---------- same interface as AuroraIndex ----------

    def setup(self) -> list[str]:
        return ["S3 index needs no setup"]

    def upsert(self, rows: list[dict]) -> int:
        """Insert or replace passages; each row has FIELDS plus 'embedding'."""
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            groups.setdefault((r["work_id"], r["version_id"]), []).append(r)
        for (work_id, ver), new in groups.items():
            old = self._get(ver, fresh=True)
            merged = {p["chunk_id"]: (p, v) for p, v in zip(old.passages, old.vectors, strict=True)}
            for r in new:
                vec = np.asarray(r["embedding"], dtype=np.float32)
                merged[r["chunk_id"]] = ({f: r.get(f) for f in FIELDS}, vec)
            items = sorted(merged.values(), key=lambda x: x[0]["chunk_id"])
            vectors = (
                np.vstack([v for _, v in items])
                if items
                else np.zeros((0, DIMENSIONS), dtype=np.float32)
            )
            self._put(work_id, ver, Shard([p for p, _ in items], vectors))
        return len(rows)

    def count(self, version_id: str) -> int:
        return len(self._get(version_id, fresh=True).passages)

    def delete_version(self, version_id: str) -> int:
        key = self._locate(version_id)
        if key is None:
            return 0
        n = len(self._get(version_id, fresh=True).passages)
        work_id = key[len(PREFIX) :].split("/", 1)[0]
        self._put(work_id, version_id, EMPTY)
        return n

    def delete_work(self, work_id: str) -> int:
        n = 0
        for key in self._list(f"{PREFIX}{work_id}/"):
            ver = key.rsplit("/", 1)[1].removesuffix(".json.gz")
            self.keys[ver] = key
            n += self.delete_version(ver)
        return n

    def _active(self, versions: list[str]) -> list[Shard]:
        return [s for s in (self._get(v) for v in versions) if s.passages]

    def keyword(self, q: str, versions: list[str], n: int) -> list[tuple[dict, float]]:
        """BM25 over the active versions, matching ANY query word (like a match query)."""
        terms = list(dict.fromkeys(tokens(q)))
        shards = self._active(versions)
        if not terms or not shards:
            return []
        docs = [
            (p, tf, ln)
            for s in shards
            for p, tf, ln in zip(s.passages, s.tf, s.lengths, strict=True)
        ]
        total, avg = len(docs), sum(ln for _, _, ln in docs) / len(docs) or 1.0
        idf = {}
        for t in terms:
            df = sum(1 for _, tf, _ in docs if t in tf)
            if df:
                idf[t] = math.log(1 + (total - df + 0.5) / (df + 0.5))
        scored = []
        for p, tf, ln in docs:
            s = sum(
                idf[t] * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * ln / avg))
                for t in idf
                if t in tf
            )
            if s > 0:
                scored.append((p, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:n]

    def semantic(
        self, vector: list[float], versions: list[str], n: int
    ) -> list[tuple[dict, float]]:
        """Cosine similarity (Titan vectors are normalized, so a dot product)."""
        shards = self._active(versions)
        if not shards:
            return []
        q = np.asarray(vector, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        passages = [p for s in shards for p in s.passages]
        matrix = np.vstack([s.vectors for s in shards])
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ q) / norms
        top = np.argsort(-scores)[:n]
        return [(passages[i], float(scores[i])) for i in top]

    def hybrid(
        self, q: str, vector: list[float], versions: list[str], n: int, keyword_weight: float
    ) -> list[tuple[dict, float]]:
        from read.retrieve import fuse

        return fuse(
            self.keyword(q, versions, n), self.semantic(vector, versions, n), keyword_weight, n
        )
