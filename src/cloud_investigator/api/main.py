"""FastAPI app — POST /api/investigate, SSE progress, inline HTML demo.

POST runs the graph synchronously (offline, deterministic cassette) and caches
the node-by-node event log; the SSE route then streams that log. The event
shapes are stable, so a live graph stream can replace the cached log without
changing the browser client.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..engine.graph import DEFAULT_SCENARIO, run_streamed
from ..sources.fake import available_scenarios

app = FastAPI(title="CloudInvestigator")

#: investigation_id -> {"report": InvestigationReport, "events": list[dict]}
_STORE: dict[str, dict] = {}


class InvestigateRequest(BaseModel):
    question: str = ""
    scenario: str = DEFAULT_SCENARIO
    demo_mode: bool = True


@app.post("/api/investigate")
def investigate(req: InvestigateRequest) -> dict:
    """Run the investigation graph synchronously (offline demo cassette)."""
    try:
        report, events = run_streamed(req.question, req.scenario)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown scenario {req.scenario!r} — available: "
                + ", ".join(available_scenarios())
            ),
        ) from exc
    _STORE[report.investigation_id] = {"report": report, "events": events}
    return report.model_dump(mode="json")


@app.get("/api/investigate/{investigation_id}/events")
async def investigate_events(investigation_id: str):
    """SSE: node-by-node progress events (node name + short payload),
    then the full report, then done."""
    entry = _STORE.get(investigation_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="unknown investigation_id — POST /api/investigate first",
        )

    async def gen():
        for ev in entry["events"]:
            yield {"data": json.dumps(ev)}
        yield {
            "data": json.dumps(
                {"node": "report", "report": entry["report"].model_dump(mode="json")}
            )
        }
        yield {"data": json.dumps({"node": "done"})}

    return EventSourceResponse(gen())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CloudInvestigator</title>
<style>
  body { font-family: ui-monospace, Menlo, Consolas, monospace; margin: 2rem; max-width: 60rem; }
  h1 { font-size: 1.2rem; }
  textarea { width: 100%; margin: .5rem 0; }
  #progress { background: #f4f4f4; padding: .75rem; min-height: 4rem; white-space: pre-wrap; }
  #report { background: #eef7ee; padding: .75rem; white-space: pre-wrap; }
</style>
</head>
<body>
<h1>CloudInvestigator <small>offline demo — S3 access-denied cassette</small></h1>
<form id="f">
  <textarea id="q" rows="3">Since this morning the QA data-lake ingestion job fails: \
qa-data-lake S3 bucket are rejected with AccessDenied. Why?</textarea>
  <button>Investigate (offline cassette)</button>
</form>
<h2>progress</h2>
<pre id="progress">idle</pre>
<h2>report</h2>
<pre id="report">—</pre>
<script>
const f = document.getElementById('f');
f.onsubmit = async (e) => {
  e.preventDefault();
  const q = document.getElementById('q').value;
  document.getElementById('progress').textContent = 'running…';
  document.getElementById('report').textContent = '…';
  const res = await fetch('/api/investigate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: q})
  });
  const report = await res.json();
  document.getElementById('progress').textContent = '';
  const es = new EventSource(
    '/api/investigate/' + encodeURIComponent(report.investigation_id) + '/events'
  );
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.node === 'done') { es.close(); return; }
    if (ev.node === 'report') {
      document.getElementById('report').textContent = JSON.stringify(ev.report, null, 2);
    } else {
      document.getElementById('progress').textContent +=
        ev.node + ' → ' + JSON.stringify(ev.payload) + '\\n';
    }
  };
};
</script>
</body>
</html>
"""
