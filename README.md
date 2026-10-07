# R.E.A.D. AWS

**Reading Educator Assistance Desk**: a proof of concept that answers K-5 teachers' Science of
Reading questions from a corpus of openly licensed research, on AWS (Bedrock, OpenSearch,
DynamoDB, Lambda).

Every answer cites its sources. The supporting excerpts and any quoted wording are sliced from
the source text by stored character offsets and hash-checked, so they never pass through the
language model. See `docs/design.md` and `CLAUDE.md` for the design and its rules.

## Run it locally

Requires Python 3.12, Docker, and an AWS profile with Bedrock access (see `infra/iam/`; replace
`ACCOUNT_ID` with your own).

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d                          # OpenSearch + DynamoDB Local
python scripts/setup_opensearch.py --local
python scripts/ingest.py corpus/<file> corpus/<file>.meta.json --local
python scripts/dev_server.py                  # http://localhost:8080
pytest
```

Source documents and their license sign-offs are not included. Add your own to `corpus/` with a
`.meta.json` sidecar (fields in `src/read/ingest.py`). A source becomes searchable only after a
person records its license verification.

## License

MIT
