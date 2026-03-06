import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

LOG_DIR = Path("logs")
TRACE_FILE = LOG_DIR / "traces.jsonl"


def ensure_log_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def utc_timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"


def write_trace(
    prompt: str,
    modules_executed: list,
    nova_request_ids: Dict[str, list],
    scores: Dict[str, float],
    decision: str,
    error: Optional[Dict[str, Any]] = None,
):
    ensure_log_dir()

    trace_entry = {
        "timestamp": utc_timestamp(),
        "problem_hash": hash_prompt(prompt),
        "modules_executed": modules_executed,
        "nova_calls": nova_request_ids,
        "scores": scores,
        "decision": decision,
        "error": error,
    }

    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace_entry) + "\n")
