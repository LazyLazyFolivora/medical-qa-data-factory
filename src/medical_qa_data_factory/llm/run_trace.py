"""Run-local, context-isolated debug events; never record authentication headers."""
import json
import time
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

_current = ContextVar("objective_trace", default=None)

@contextmanager
def trace_run(root, run_id):
    token = _current.set({"root": Path(root) / "debug_trace", "run_id": run_id})
    try:
        yield
    finally:
        _current.reset(token)

def trace_step(**fields):
    current = _current.get()
    if current is not None:
        _current.set({"root": current["root"], "run_id": current["run_id"], **fields})

def trace_event(kind, **data):
    current = _current.get()
    if current is None:
        return
    stamp = time.time_ns()
    payload = {**{k: v for k, v in current.items() if k != "root"},
               "timestamp_ns": stamp, "event": kind, **data}
    try:
        current["root"].mkdir(parents=True, exist_ok=True)
        path = current["root"] / f"{stamp}-{uuid4().hex}-{kind}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        warnings.warn(f"Could not write debug trace: {exc}", RuntimeWarning)
