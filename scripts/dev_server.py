"""Local dev server: the same app the API Lambda runs, against the deployed AWS stack.

    python scripts/stack_env.py      once per deploy: writes .env.aws from the stack outputs
    python scripts/dev_server.py     then open http://localhost:8080

Runs only on this PC (127.0.0.1) with the read-poc AWS profile. There is no passcode here:
never expose this port. Ingest and review jobs run in threads instead of the worker Lambda.
Calls to Bedrock cost money (a question is about 2 cents with Nova Pro, a review about 45).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
ENV_FILE = ROOT / ".env.aws"


if __name__ == "__main__":
    import uvicorn

    from read.jobs import Env, load_env_file

    load_env_file(ENV_FILE)
    os.environ.pop("WORKER_FUNCTION", None)  # local jobs run in threads

    from read.webapp import create_app

    app = create_app(Env.from_environ(profile=os.environ.get("AWS_PROFILE", "read-poc")))
    uvicorn.run(app, host="127.0.0.1", port=8080)
