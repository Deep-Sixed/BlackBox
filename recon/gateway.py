"""Recon observer gateway — POST /v1/record on 127.0.0.1:8773.

Contract (unchanged from the AgentSync deployment; the universal stop hook
at ~/.config/Claude/hooks/evecor-contextforge-stop.sh depends on it):

    {"surface": "...", "request_id": "...", "tool": "...", "result": {...}}

Every event is appended to the observations journal. Events whose tool is
"session_complete" additionally finalize a full Recon session record for
the reported cwd. Repeat stop events for the same session dedup via the
request-ID reservation and report status "duplicate".
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from . import config
from .finalize import finalize_session

app = FastAPI(title="recon-gateway", docs_url=None, redoc_url=None)


class Observation(BaseModel):
    surface: str = "unknown"
    request_id: str = ""
    tool: str = ""
    result: dict[str, Any] = {}


def append_observation(entry: dict[str, Any]) -> None:
    log_path = config.observations_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/record")
def record(observation: Observation) -> dict[str, Any]:
    entry = {
        "observation_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "surface": observation.surface,
        "request_id": observation.request_id,
        "tool": observation.tool,
        "result_summary": json.dumps(observation.result, sort_keys=True),
    }
    append_observation(entry)

    if observation.tool != "session_complete":
        return {"status": "recorded", "observation_id": entry["observation_id"]}

    session_id = observation.request_id or f"session-{uuid.uuid4().hex[:8]}"
    cwd = observation.result.get("cwd", "")
    if not cwd:
        return {"status": "skipped", "reason": "no cwd in session_complete event"}

    outcome = finalize_session(
        surface=observation.surface,
        session_id=session_id,
        cwd=cwd,
        model_or_tool=observation.result.get("model", "default"),
    )
    return {"observation_id": entry["observation_id"], **outcome}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8773, log_level="warning")


if __name__ == "__main__":
    main()
