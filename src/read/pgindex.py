"""Passage index on Aurora PostgreSQL Serverless v2: pgvector + full-text, via the RDS Data API.

Replaces the OpenSearch passages index (D90). Each passage row holds the same fields the
OpenSearch documents had, plus its embedding and a generated full-text vector. Hybrid search
runs the keyword and vector queries separately, then retrieve.fuse() normalizes and combines
the scores the way OpenSearch's hybrid-norm pipeline did (min-max, weighted mean).

The cluster scales to zero when idle; the first call after a pause gets a
DatabaseResumingException, so every statement retries for up to RESUME_WAIT seconds.
"""

import json
import re
import time

from read.embed import DIMENSIONS

DATABASE = "read"
RESUME_WAIT = 90  # seconds to wait for a paused cluster to resume
BATCH = 10  # rows per BatchExecuteStatement (a 1024-float vector is ~12 KB of SQL text)
FIELDS = (
    "chunk_id",
    "work_id",
    "version_id",
    "section_id",
    "char_start",
    "char_end",
    "text",
    "text_sha256",
    "title",
    "section_path",
    "page",
    "page_end",
    "embed_model",
)
INTS = {"char_start", "char_end", "page", "page_end"}

SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""CREATE TABLE IF NOT EXISTS passages (
        chunk_id text PRIMARY KEY,
        work_id text NOT NULL,
        version_id text NOT NULL,
        section_id text NOT NULL,
        char_start integer NOT NULL,
        char_end integer NOT NULL,
        text text NOT NULL,
        text_sha256 text NOT NULL,
        title text,
        section_path text,
        page integer,
        page_end integer,
        embed_model text,
        embedding vector({DIMENSIONS}) NOT NULL,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
    )""",
    "CREATE INDEX IF NOT EXISTS passages_tsv ON passages USING gin (tsv)",
    "CREATE INDEX IF NOT EXISTS passages_version ON passages (version_id)",
    "CREATE INDEX IF NOT EXISTS passages_work ON passages (work_id)",
]
COLUMNS = ", ".join(FIELDS)


def or_query(q: str) -> str:
    """A to_tsquery string matching ANY of the query's words, like OpenSearch's match query
    (plainto_tsquery would require all of them). Only letters and digits reach Postgres."""
    words = re.findall(r"[A-Za-z0-9]+", q.lower())
    return " | ".join(dict.fromkeys(words))


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def param(name: str, value) -> dict:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": name, "value": {"doubleValue": value}}
    return {"name": name, "value": {"stringValue": str(value)}}


def resuming(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "DatabaseResumingException" or "resuming" in str(exc).lower()


class AuroraIndex:
    """The passages index. client is a boto3 rds-data client."""

    def __init__(self, client, cluster_arn: str, secret_arn: str, database: str = DATABASE):
        self.client, self.cluster, self.secret, self.database = (
            client,
            cluster_arn,
            secret_arn,
            database,
        )

    def _call(self, fn, **kw):
        deadline = time.monotonic() + RESUME_WAIT
        while True:
            try:
                return fn(
                    resourceArn=self.cluster, secretArn=self.secret, database=self.database, **kw
                )
            except Exception as exc:
                if not resuming(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(5)

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        res = self._call(
            self.client.execute_statement,
            sql=sql,
            parameters=[param(k, v) for k, v in (params or {}).items()],
            formatRecordsAs="JSON",
        )
        return json.loads(res.get("formattedRecords") or "[]")

    def execute(self, sql: str, params: dict | None = None) -> int:
        res = self._call(
            self.client.execute_statement,
            sql=sql,
            parameters=[param(k, v) for k, v in (params or {}).items()],
        )
        return int(res.get("numberOfRecordsUpdated", 0))

    def setup(self) -> list[str]:
        """Create the extension, table, and indexes if missing (idempotent)."""
        for sql in SCHEMA:
            self.execute(sql)
        return [f"ran {len(SCHEMA)} schema statements"]

    def upsert(self, rows: list[dict]) -> int:
        """Insert or replace passages; each row has FIELDS plus 'embedding'."""
        sql = (
            f"INSERT INTO passages ({COLUMNS}, embedding) VALUES ("
            + ", ".join(f":{f}" for f in FIELDS)
            + ", CAST(:embedding AS vector)) ON CONFLICT (chunk_id) DO UPDATE SET "
            + ", ".join(f"{f} = EXCLUDED.{f}" for f in FIELDS if f != "chunk_id")
            + ", embedding = EXCLUDED.embedding"
        )
        n = 0
        for k in range(0, len(rows), BATCH):
            sets = []
            for r in rows[k : k + BATCH]:
                values = {
                    f: (int(r[f]) if f in INTS and r.get(f) is not None else r.get(f))
                    for f in FIELDS
                }
                values["embedding"] = vector_literal(r["embedding"])
                sets.append([param(f, v) for f, v in values.items()])
            self._call(self.client.batch_execute_statement, sql=sql, parameterSets=sets)
            n += len(sets)
        return n

    def count(self, version_id: str) -> int:
        rows = self.query(
            "SELECT count(*) AS n FROM passages WHERE version_id = :v", {"v": version_id}
        )
        return int(rows[0]["n"]) if rows else 0

    def delete_version(self, version_id: str) -> int:
        return self.execute("DELETE FROM passages WHERE version_id = :v", {"v": version_id})

    def delete_work(self, work_id: str) -> int:
        return self.execute("DELETE FROM passages WHERE work_id = :w", {"w": work_id})

    def keyword(self, q: str, versions: list[str], n: int) -> list[tuple[dict, float]]:
        terms = or_query(q)
        if not terms or not versions:
            return []
        rows = self.query(
            f"SELECT {COLUMNS}, ts_rank_cd(tsv, query) AS score "
            "FROM passages, to_tsquery('english', :q) AS query "
            "WHERE tsv @@ query AND version_id = ANY(string_to_array(:v, ',')) "
            "ORDER BY score DESC LIMIT :n",
            {"q": terms, "v": ",".join(versions), "n": n},
        )
        return [({f: r.get(f) for f in FIELDS}, float(r["score"])) for r in rows]

    def semantic(
        self, vector: list[float], versions: list[str], n: int
    ) -> list[tuple[dict, float]]:
        if not versions:
            return []
        rows = self.query(
            f"SELECT {COLUMNS}, 1 - (embedding <=> CAST(:e AS vector)) AS score "
            "FROM passages WHERE version_id = ANY(string_to_array(:v, ',')) "
            "ORDER BY embedding <=> CAST(:e AS vector) LIMIT :n",
            {"e": vector_literal(vector), "v": ",".join(versions), "n": n},
        )
        return [({f: r.get(f) for f in FIELDS}, float(r["score"])) for r in rows]

    def hybrid(
        self, q: str, vector: list[float], versions: list[str], n: int, keyword_weight: float
    ) -> list[tuple[dict, float]]:
        from read.retrieve import fuse

        return fuse(
            self.keyword(q, versions, n), self.semantic(vector, versions, n), keyword_weight, n
        )
