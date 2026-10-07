"""Ingest one source file with its meta.json sidecar.

    python scripts/ingest.py corpus/<file> corpus/<file>.meta.json --local

--local uses the Docker services (OpenSearch at :9200, DynamoDB Local at :8000) and writes
canonical text to data/works-text/. Embeddings call Bedrock Titan with the read-poc profile
(about $0.02 per million tokens; the estimate is printed first). The steps are in
src/read/pipeline.py; the dev server's Sources page runs the same code.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from read.pipeline import IngestError, ingest_file  # noqa: E402
from read.service import Stores  # noqa: E402
from read.store import ensure_index  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("meta")
    ap.add_argument("--local", action="store_true", help="use the local Docker services")
    ap.add_argument("--profile", default="read-poc", help="AWS profile for Bedrock embeddings")
    args = ap.parse_args()
    if not args.local:
        sys.exit("failed: only --local is supported until the AWS stack exists")
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    st = Stores.local(ROOT, args.profile)
    ensure_index(st.os)
    try:
        res = ingest_file(
            Path(args.file).read_bytes(),
            Path(args.file).name,
            meta,
            st,
            progress=lambda m: print(f"{meta.get('work_id')}: {m}"),
        )
    except IngestError as e:
        sys.exit(f"failed: {e}")
    print(f"{res['work_id']}: {res['status']}")


if __name__ == "__main__":
    main()
