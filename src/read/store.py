"""Storage on AWS: DynamoDB tables, S3 buckets for canonical text, sources, review material,
and app files (D90). Passages live in Aurora PostgreSQL (read.pgindex).

The local file-backed stores at the bottom are kept for unit tests and offline use; nothing
in R.E.A.D. needs Docker any more.
"""

import json
import os
import uuid

KEYWORD_WEIGHT, SEMANTIC_WEIGHT = 0.3, 0.7  # hybrid search weights (retrieve.fuse)

# ---- DynamoDB: works and sections tables ----

WORKS_TABLE = os.environ.get("WORKS_TABLE", "read-poc-works")
SECTIONS_TABLE = os.environ.get("SECTIONS_TABLE", "read-poc-sections")


class WriteOnceError(Exception):
    """A canonical text object already exists with different content."""


def _missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")


# ---- S3 stores ----


class S3TextStore:
    """works-text/<work_id>/<version_id>.txt in S3, write-once (the bucket is versioned)."""

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def key(self, work_id: str, version_id: str) -> str:
        return f"{work_id}/{version_id}.txt"

    def put(self, work_id: str, version_id: str, text: str) -> str:
        key, data = self.key(work_id, version_id), text.encode("utf-8")
        try:
            old = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            if not _missing(exc):
                raise
            old = None
        if old is not None:
            if old != data:
                raise WriteOnceError(f"s3://{self.bucket}/{key} exists with different content")
            return key
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType="text/plain; charset=utf-8"
        )
        return key

    def get(self, work_id: str, version_id: str) -> str:
        obj = self.s3.get_object(Bucket=self.bucket, Key=self.key(work_id, version_id))
        return obj["Body"].read().decode("utf-8")


class S3TempStore:
    """Submitted material, review state and results under <rid>/ in the plans-temp bucket,
    whose lifecycle rule deletes everything after a day (rule 7). Same interface as TempStore."""

    def __init__(self, s3, bucket: str, hours: float = 24):
        self.s3, self.bucket, self.hours = s3, bucket, hours

    def new(self) -> str:
        rid = uuid.uuid4().hex[:16]
        self.put_json(rid, ".created", {})
        return rid

    def _key(self, rid: str, name: str) -> str:
        if not rid.isalnum() or "/" in name:
            raise KeyError(rid)
        return f"{rid}/{name}"

    def exists(self, rid: str, name: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(rid, name))
            return True
        except Exception as exc:
            if _missing(exc) or "Not Found" in str(exc) or "404" in str(exc):
                return False
            raise

    def put_json(self, rid: str, name: str, value) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(rid, name),
            Body=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    def get_json(self, rid: str, name: str):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(rid, name))
        except Exception as exc:
            if _missing(exc):
                raise FileNotFoundError(f"{rid}/{name}") from None
            raise
        return json.loads(obj["Body"].read().decode("utf-8"))

    def purge(self, now: float | None = None) -> int:
        return 0  # the bucket's lifecycle rule deletes objects after a day


class S3FileStore:
    """Plain files in one bucket: uploaded sources and their meta.json sidecars (works-raw),
    or app files such as the edited checklist."""

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            if _missing(exc):
                return None
            raise

    def put_json(self, key: str, value) -> None:
        self.put(
            key,
            (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            "application/json",
        )

    def get_json(self, key: str):
        data = self.get(key)
        return json.loads(data.decode("utf-8")) if data is not None else None


# ---- Local file stores: unit tests and offline use only ----


class LocalTextStore:
    """works-text/<work_id>/<version_id>.txt on local disk, write-once like the S3 store."""

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


class TempStore:
    """Local version of S3TempStore: material kept at most `hours`, never indexed (rule 7)."""

    def __init__(self, root: str | os.PathLike, hours: float = 24):
        from pathlib import Path

        self.root, self.hours = Path(root), hours

    def new(self) -> str:
        rid = uuid.uuid4().hex[:16]
        (self.root / rid).mkdir(parents=True)
        return rid

    def path(self, rid: str, name: str):
        if not rid.isalnum() or not self.root.joinpath(rid).is_dir():
            raise KeyError(rid)
        return self.root / rid / name

    def exists(self, rid: str, name: str) -> bool:
        try:
            return self.path(rid, name).exists()
        except KeyError:
            return False

    def put_json(self, rid: str, name: str, value) -> None:
        self.path(rid, name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def get_json(self, rid: str, name: str):
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
