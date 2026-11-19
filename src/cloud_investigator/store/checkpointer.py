"""LangGraph checkpointer on Postgres — the restart seam.

``thread_id = investigation_id``: one checkpoint thread per investigation, so
an interrupted run resumes exactly where it stopped (AGENTS §10 restart drill).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .. import contracts
from ..config import load_settings

#: The pinned contract models are the only custom types that ever enter graph
#: state — register them so checkpoint (de)serialization is explicit instead
#: of relying on langgraph's unregistered-type fallback (which warns today
#: and is slated to be blocked in future versions). Each class normalizes to
#: an allowed ``("cloud_investigator.contracts", "<Class>")`` key.
ALLOWED_MSGPACK_TYPES = (
    contracts.TimeRange,
    contracts.ResourceRef,
    contracts.EvidenceItem,
    contracts.Citation,
    contracts.Finding,
    contracts.Hypothesis,
    contracts.TimelineEvent,
    contracts.CoverageReport,
    contracts.InvestigationReport,
    contracts.ProbePlan,
)

#: Entered ``from_conn_string`` contexts, keyed by the saver they yielded.
#: The context must be referenced for as long as the saver lives — a
#: garbage-collected generator context would close the underlying connection.
_OPEN: dict[int, object] = {}


def _configure_serde(saver: PostgresSaver) -> None:
    saver.serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_TYPES)


def build_postgres_checkpointer(database_url: str | None = None) -> PostgresSaver:
    """Eager builder for scripts/tests: open the connection NOW, run the
    idempotent ``.setup()``, return the live saver.

    The connection stays open for the process lifetime; call
    ``close_checkpointer(saver)`` for deterministic teardown, or use the
    ``postgres_checkpointer(...)`` context manager instead.
    """
    url = database_url or load_settings().database_url
    context = PostgresSaver.from_conn_string(url)
    saver = context.__enter__()
    _OPEN[id(saver)] = context
    _configure_serde(saver)
    saver.setup()  # idempotent: creates/migrates checkpoint tables
    return saver


@contextmanager
def postgres_checkpointer(database_url: str | None = None) -> Iterator[PostgresSaver]:
    """Context-managed form: opens, sets up, yields, closes on exit."""
    url = database_url or load_settings().database_url
    with PostgresSaver.from_conn_string(url) as saver:
        _configure_serde(saver)
        saver.setup()
        yield saver


def close_checkpointer(saver: PostgresSaver) -> None:
    """Deterministically close a checkpointer built by ``build_postgres_checkpointer``."""
    context = _OPEN.pop(id(saver), None)
    if context is not None:
        context.__exit__(None, None, None)  # type: ignore[attr-defined]
    else:
        conn = getattr(saver, "conn", None)
        if conn is not None:
            conn.close()
