"""Create the passages index and hybrid-norm search pipeline.

    python scripts/setup_opensearch.py --local     # Docker OpenSearch at localhost:9200 (free)

Creating the AWS OpenSearch Serverless collection is not in this script yet: it bills about
$0.24 per OCU-hour while it exists, and John sets up that test himself (docs/decisions.md).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from read.store import ensure_index, opensearch_client  # noqa: E402

LOCAL = "http://localhost:9200"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", action="store_true", help=f"use local OpenSearch at {LOCAL}")
    args = ap.parse_args()
    if not args.local:
        sys.exit("Only --local is supported for now (AWS collection needs John's approval).")
    client = opensearch_client(LOCAL)
    info = client.info()
    print(f"OpenSearch {info['version']['number']} at {LOCAL}")
    for step in ensure_index(client):
        print(step)


if __name__ == "__main__":
    main()
