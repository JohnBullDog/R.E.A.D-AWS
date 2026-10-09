"""Create the passages table and indexes in Aurora (idempotent; run once after deploying).

    python scripts/setup_aurora.py

Uses .env.aws (scripts/stack_env.py). The first call may wait while the cluster resumes.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from read.jobs import load_env_file  # noqa: E402
from read.service import Stores  # noqa: E402


def main() -> None:
    load_env_file(ROOT / ".env.aws")
    st = Stores.aws(os.environ.get("AWS_PROFILE", "read-poc"))
    for line in st.index.setup():
        print(line)
    n = st.index.query("SELECT count(*) AS n FROM passages")[0]["n"]
    print(f"passages table ready ({n} rows)")


if __name__ == "__main__":
    main()
