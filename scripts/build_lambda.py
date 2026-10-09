"""Stage the Lambda bundle in .build/lambda for sam build (template.yaml CodeUri).

    python scripts/build_lambda.py
    sam build

Copies the read package, the handlers, the web pages, and the default checklist; sam build
then installs requirements-lambda.txt (staged as requirements.txt) for the Lambda platform.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".build" / "lambda"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(ROOT / "src" / "read", OUT / "read", ignore=IGNORE)
    shutil.copytree(ROOT / "src" / "handlers", OUT / "handlers", ignore=IGNORE)
    shutil.copytree(ROOT / "web", OUT / "web", ignore=IGNORE)
    (OUT / "rubric").mkdir()
    shutil.copy2(ROOT / "rubric" / "checklist.json", OUT / "rubric" / "checklist.json")
    shutil.copy2(ROOT / "requirements-lambda.txt", OUT / "requirements.txt")
    files = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(f"staged {files} files in {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
