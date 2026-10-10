"""Ingestion rules: metadata checks, the license gate, version IDs, work records.

The orchestration (extract, chunk, embed, index, store, activate) is scripts/ingest.py.
See docs/design.md, "Ingestion" and "Work registry".
"""

import hashlib
import re
from datetime import date, datetime

from read.retrieve import AUTHORITY

REQUIRED_META = ("work_id", "title", "publisher", "url", "pub_date", "doc_type")
LICENSE_FIELDS = ("license", "license_verified_by", "license_verified_on")
WORK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
PUB_DATE = re.compile(r"^\d{4}(-\d{2})?$")
URL = re.compile(r"^https?://[^\s<>\"']+$", re.I)  # no javascript:/data: links (stored XSS)
META_TEXT_MAX = 1000  # any single text field in meta.json


def check_meta(meta: dict) -> list[str]:
    """Problems that stop ingestion ("failed: missing metadata"); empty means OK."""
    problems = [f"missing {f}" for f in REQUIRED_META if not meta.get(f)]
    if meta.get("work_id") and not WORK_ID.match(meta["work_id"]):
        problems.append("work_id must be lowercase letters, digits and hyphens")
    if meta.get("pub_date") and not PUB_DATE.match(str(meta["pub_date"])):
        problems.append("pub_date must be YYYY or YYYY-MM")
    if meta.get("doc_type") and meta["doc_type"] not in AUTHORITY:
        problems.append(f"doc_type must be one of {sorted(AUTHORITY)}")
    if meta.get("url") and not (len(str(meta["url"])) <= 2000 and URL.match(str(meta["url"]))):
        problems.append("url must be an http:// or https:// address")
    if meta.get("superseded_by") and not WORK_ID.match(str(meta["superseded_by"])):
        problems.append("superseded_by must be a source ID")
    long = [k for k, v in meta.items() if isinstance(v, str) and len(v) > META_TEXT_MAX]
    if long:
        problems.append(f"too long (over {META_TEXT_MAX} characters): {', '.join(sorted(long))}")
    if meta.get("expires_on"):
        try:
            date.fromisoformat(meta["expires_on"])
        except ValueError:
            problems.append("expires_on must be YYYY-MM-DD")
    return problems


def license_ok(meta: dict) -> bool:
    """The license gate (CLAUDE.md rule 6): all three fields set by a person, with a real date.

    Drafts written by Claude leave license_verified_by/on empty, so they never pass.
    """
    if not all(isinstance(meta.get(f), str) and meta[f].strip() for f in LICENSE_FIELDS):
        return False
    try:
        date.fromisoformat(meta["license_verified_on"])
    except ValueError:
        return False
    return True


def local_version_id(data: bytes) -> str:
    """Stand-in for the S3 version ID when running locally: a hash of the source bytes.

    Re-ingesting an unchanged file gives the same ID, so it is detected as unchanged.
    """
    return "sha-" + hashlib.sha256(data).hexdigest()[:16]


def work_record(
    meta: dict,
    version_id: str,
    source_key: str,
    canonical_key: str,
    passage_count: int,
    status: str,
    active_version_id: str | None = None,
    activated_at: str | None = None,
) -> dict:
    """The works-table row: tagging from meta.json plus processing state."""
    tags = (
        "title",
        "publisher",
        "url",
        "pub_date",
        "version",
        "doc_type",
        "peer_reviewed",
        "grade_bands",
        "components",
        "superseded_by",
        "expires_on",
        *LICENSE_FIELDS,
        "placeholder",
    )
    row = {k: meta[k] for k in tags if meta.get(k) not in (None, "", [])}
    row.update(
        work_id=meta["work_id"],
        source_key=source_key,
        source_version_id=version_id,
        canonical_key=canonical_key,
        passage_count=passage_count,
        status=status,
    )
    if active_version_id:
        row["active_version_id"] = active_version_id
    if activated_at:
        row["activated_at"] = activated_at
    return row


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
