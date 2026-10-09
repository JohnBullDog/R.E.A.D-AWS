"""Write .env.aws from the read-poc stack's outputs, for the local dev server and scripts.

    python scripts/stack_env.py [--profile read-poc]

.env.aws holds resource names and ARNs only (no secrets) and is git-ignored.
"""

import argparse
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
STACK = "read-poc"
KEYS = (
    "DB_CLUSTER_ARN",
    "DB_SECRET_ARN",
    "TEXT_BUCKET",
    "RAW_BUCKET",
    "TEMP_BUCKET",
    "APP_BUCKET",
    "WORKS_TABLE",
    "SECTIONS_TABLE",
    "ANSWER_MODEL_ID",
    "API_URL",
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="read-poc")
    args = ap.parse_args()
    cf = boto3.Session(profile_name=args.profile, region_name="us-east-1").client("cloudformation")
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    }
    lines = [f"AWS_PROFILE={args.profile}"]
    for k in KEYS:
        name = "".join(
            w.capitalize() for w in k.lower().split("_")
        )  # DB_CLUSTER_ARN -> DbClusterArn
        if name in outputs:
            lines.append(f"{k}={outputs[name]}")
    (ROOT / ".env.aws").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote .env.aws ({len(lines) - 1} values); site: {outputs.get('ApiUrl', '?')}")


if __name__ == "__main__":
    main()
