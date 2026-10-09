"""Worker Lambda: runs ingest and review jobs the API Lambda hands it (async invoke)."""

from read import jobs

ENV = None


def handler(event: dict, context) -> dict:
    global ENV
    if ENV is None:
        ENV = jobs.Env.from_environ()
    jobs.handle(ENV, event)
    return {"ok": True}
