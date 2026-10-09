"""Ingest one source file with its meta.json sidecar into the AWS stack.

    python scripts/ingest.py corpus/<file> corpus/<file>.meta.json

Copies the file and its meta.json to the private sources bucket (so the Sources page can show
and sign off the license), then runs src/read/pipeline.py here: canonical text to S3, passages
to Aurora, works/sections to DynamoDB. Use this for files over the 5 MB page-upload limit.
Embeddings call Bedrock Titan (about $0.02 per million tokens; the estimate is printed first).
Uses .env.aws (scripts/stack_env.py).
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from read.jobs import load_env_file  # noqa: E402
from read.pipeline import IngestError, ingest_file  # noqa: E402
from read.service import Stores  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("meta")
    args = ap.parse_args()
    load_env_file(ROOT / ".env.aws")

    import boto3

    from read.store import S3FileStore

    profile = os.environ.get("AWS_PROFILE", "read-poc")
    path = Path(args.file)
    data = path.read_bytes()
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    s3 = boto3.Session(profile_name=profile, region_name="us-east-1").client("s3")
    raw = S3FileStore(s3, os.environ["RAW_BUCKET"])
    raw.put(path.name, data)
    raw.put_json(f"{path.name}.meta.json", meta)
    try:
        res = ingest_file(
            data,
            path.name,
            meta,
            Stores.aws(profile),
            progress=lambda m: print(f"{meta.get('work_id')}: {m}"),
        )
    except IngestError as e:
        sys.exit(f"failed: {e}")
    print(f"{res['work_id']}: {res['status']}")


if __name__ == "__main__":
    main()
