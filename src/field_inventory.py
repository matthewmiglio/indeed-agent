"""Field inventory tracker for iterating on Indeed form coverage.

Records every form field encountered across runs so we can audit which field
labels, types, and option-sets the agent has seen — and which were handled by
profile-keyword mapping vs. fell through to the LLM. Used in --dry-run mode
to build up coverage without submitting applications.

Files written to data/debug/:
  - field_inventory.jsonl   : one line per unique (label, type, options) signature
  - unhandled_fields.jsonl  : fields where keyword mapping returned None
  - run_summary.json        : per-run rollup of jobs / pages / fields / handlers
"""

import json
import os
import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Any

DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "debug")
INVENTORY_PATH = os.path.join(DEBUG_DIR, "field_inventory.jsonl")
UNHANDLED_PATH = os.path.join(DEBUG_DIR, "unhandled_fields.jsonl")
SUMMARY_PATH = os.path.join(DEBUG_DIR, "run_summary.json")


def _signature(label: str, ftype: str, options: list[str]) -> str:
    """Stable fingerprint of a field so we don't re-log the same one."""
    blob = json.dumps({"l": label.strip().lower(), "t": ftype, "o": sorted(options)}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _load_seen() -> set[str]:
    """Load the set of field signatures already in field_inventory.jsonl."""
    seen = set()
    if os.path.exists(INVENTORY_PATH):
        with open(INVENTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    seen.add(rec.get("signature", ""))
                except json.JSONDecodeError:
                    continue
    return seen


@dataclass
class RunStats:
    """Per-run rollup written to run_summary.json on flush()."""
    started_at: str = ""
    finished_at: str = ""
    mode: str = "live"            # "live" or "dry-run"
    jobs_attempted: int = 0
    jobs_completed: int = 0       # walked to submit (or would-have-submit)
    jobs_failed: int = 0
    pages_traversed: int = 0
    fields_total: int = 0
    fields_by_type: dict[str, int] = field(default_factory=dict)
    handled_by_keyword: int = 0
    handled_by_llm: int = 0
    unhandled_required: int = 0
    new_signatures_this_run: int = 0
    notes: list[str] = field(default_factory=list)


_STATS = RunStats()
_SEEN: set[str] = set()


def init_run(mode: str = "live"):
    """Reset in-memory stats for a fresh run."""
    global _STATS, _SEEN
    _STATS = RunStats(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        mode=mode,
    )
    _SEEN = _load_seen()
    os.makedirs(DEBUG_DIR, exist_ok=True)


def note(msg: str):
    """Add a free-text note to the run summary."""
    _STATS.notes.append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def record_page(fields: list[Any], job_url: str = "", step: int = 0):
    """Record every field on a freshly-analyzed form page.

    `fields` is a list of FormField (duck-typed: label_text, field_type, options, required).
    """
    _STATS.pages_traversed += 1
    new_this_call = 0
    for f in fields:
        sig = _signature(f.label_text or "", f.field_type, f.options)
        _STATS.fields_total += 1
        _STATS.fields_by_type[f.field_type] = _STATS.fields_by_type.get(f.field_type, 0) + 1
        if sig in _SEEN:
            continue
        _SEEN.add(sig)
        new_this_call += 1
        rec = {
            "signature": sig,
            "label": f.label_text,
            "type": f.field_type,
            "options": f.options,
            "required": f.required,
            "selector": f.selector,
            "first_seen_url": job_url,
            "first_seen_step": step,
            "first_seen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(INVENTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    _STATS.new_signatures_this_run += new_this_call


def record_handler(field_obj: Any, handler: str, value: Any = None):
    """Record how a field got answered. handler ∈ {keyword, llm, unhandled, prefilled}."""
    if handler == "keyword":
        _STATS.handled_by_keyword += 1
    elif handler == "llm":
        _STATS.handled_by_llm += 1
        # Persist what the LLM answered so we can review and codify common ones
        rec = {
            "label": field_obj.label_text,
            "type": field_obj.field_type,
            "options": field_obj.options,
            "required": field_obj.required,
            "llm_answer": value,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(UNHANDLED_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    elif handler == "unhandled" and field_obj.required:
        _STATS.unhandled_required += 1


def record_job_start():
    _STATS.jobs_attempted += 1


def record_job_completed():
    _STATS.jobs_completed += 1


def record_job_failed():
    _STATS.jobs_failed += 1


def flush():
    """Write run_summary.json and print a human-readable rollup."""
    _STATS.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(_STATS), f, indent=2)

    print("\n" + "=" * 60)
    print("FIELD INVENTORY — RUN SUMMARY")
    print("=" * 60)
    print(f"  Mode:                  {_STATS.mode}")
    print(f"  Jobs attempted:        {_STATS.jobs_attempted}")
    print(f"  Jobs completed:        {_STATS.jobs_completed}")
    print(f"  Jobs failed:           {_STATS.jobs_failed}")
    print(f"  Form pages traversed:  {_STATS.pages_traversed}")
    print(f"  Fields seen (total):   {_STATS.fields_total}")
    print(f"  New signatures:        {_STATS.new_signatures_this_run}")
    print(f"  Handled by keyword:    {_STATS.handled_by_keyword}")
    print(f"  Handled by LLM:        {_STATS.handled_by_llm}")
    print(f"  Unhandled required:    {_STATS.unhandled_required}")
    print(f"  Field types: {_STATS.fields_by_type}")
    print(f"  Inventory file: {INVENTORY_PATH}")
    print(f"  Summary file:   {SUMMARY_PATH}")
    print("=" * 60)
