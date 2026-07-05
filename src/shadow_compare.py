"""Shadow A/B verdict comparison for the daily fuzzy call (cost lever #5).

Moving the batched ``resolve_fuzzy`` call from Sonnet to Haiku is the biggest
remaining raw cost lever (~3x on paper), but the tasks lean on the
"be conservative, false positives are worse" judgement a smaller model is
likeliest to slip on — so the swap is gated on a week of side-by-side verdict
diffing (issue #16). When ``SHADOW_MODEL`` is set, ``claude_fuzzy`` sends the
SAME payload to the shadow model right after the primary call and this module
diffs the two ``submit_results`` tool inputs. The digest is built exclusively
from the primary result; the shadow run only produces a comparison record.

Per-run records accumulate in ``shadow_runs.json`` (14th Gist file)::

    {
      "runs": [
        {
          "at": "<iso ts>",
          "primary_model": "claude-sonnet-4-6",
          "shadow_model": "claude-haiku-4-5-20251001",
          "summary": {"total": 14, "agree": 12,
                      "by_type": {"shop_sales": {"total": 9, "agree": 8}, ...}},
          "disagreements": [{"type", "key", "primary": {...}|null,
                             "shadow": {...}|null}, ...],
          "primary_usage": {...} | null,
          "shadow_usage": {...} | null
        },
        ...
      ]
    }

Only DISAGREEMENTS carry full verdict pairs — agreements are counted, not
stored, so a week of runs stays small. After the week,
``python -m src.shadow_report`` renders the accumulated store into agreement
rates, a false-positive risk split, and a cost comparison.

What counts as agreement (per task type, entries matched by ``id``):

  * shop_sales     — same ``status``. Descriptions are free-form wording and
                     never compared.
  * email_sales    — same ``status`` AND same ``starts_on``/``ends_on`` (the
                     dates drive the email-sale persistence window, so a date
                     slip is a real disagreement even when the status matches).
  * resolutions    — same ``url`` (trailing-slash-insensitive). Confidence is
                     recorded but not scored.
  * loose_matches  — same ``matched_url`` (same normalisation).

An entry present in one response but missing from the other (e.g. the shadow
response truncated at the output cap) counts as a disagreement with the
missing side ``null``.

This module is pure (no Claude, no I/O) — it mirrors the
``src/shop_verdicts.py`` lifecycle: ``compare`` on the read side, ``append_run``
+ ``prune`` on the write side, ``summarize`` for the report CLI.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

# Runs older than this are dropped on the daily prune. The A/B experiment needs
# ~a week; 30 days keeps the whole experiment reviewable without letting an
# accidentally-left-on shadow run grow the Gist file forever.
_RETENTION_DAYS = 30

# Per task type: (decision fields scored for agreement, context fields carried
# into disagreement records for readability, whether a field is a URL).
_DECISION_FIELDS = {
    "shop_sales": ("status",),
    "email_sales": ("status", "starts_on", "ends_on"),
    "resolutions": ("url",),
    "loose_matches": ("matched_url",),
}
_CONTEXT_FIELDS = {
    "shop_sales": ("shop", "description"),
    "email_sales": ("shop", "email_id", "description"),
    "resolutions": ("shop_name", "confidence"),
    "loose_matches": ("mention", "shop", "confidence"),
}
_URL_FIELDS = frozenset({"url", "matched_url"})
# Human-readable key for a disagreement record, per type (first present wins).
_KEY_FIELDS = {
    "shop_sales": ("shop",),
    "email_sales": ("shop", "email_id"),
    "resolutions": ("shop_name",),
    "loose_matches": ("mention",),
}


def _norm(field: str, value: object) -> str:
    """Normalise a decision-field value for equality comparison.

    Statuses/dates: stripped + lowercased (statuses are a fixed enum, dates are
    ISO strings — case never matters). URLs: stripped, trailing slash dropped —
    both models pick from the SAME candidate list in the payload, so anything
    beyond trailing-slash tolerance would mask a genuinely different pick.
    None and empty string both normalise to "" (absent decision).
    """
    s = str(value).strip() if value is not None else ""
    if field in _URL_FIELDS:
        return s.rstrip("/")
    return s.lower()


def _slim(entry: dict, task_type: str) -> dict:
    """Decision + context fields of one entry (id dropped) for the record."""
    fields = _DECISION_FIELDS[task_type] + _CONTEXT_FIELDS[task_type]
    return {f: entry.get(f) for f in fields if f in entry}


def _index_by_id(entries: object) -> dict[str, dict]:
    """{id: entry} for the dict entries that carry an id; junk skipped."""
    out: dict[str, dict] = {}
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("id"):
            out[str(e["id"])] = e
    return out


def _entry_key(entry: dict | None, task_type: str) -> str:
    for f in _KEY_FIELDS[task_type]:
        if entry and entry.get(f):
            return str(entry[f])
    return "?"


def compare(primary: dict, shadow: dict) -> dict:
    """Diff two ``submit_results`` tool inputs (same payload, two models).

    Returns ``{"summary": {total, agree, by_type}, "disagreements": [...]}``.
    Entries are matched by the ``id`` echoed back from the shared payload;
    an id present on only one side is a disagreement with the other side None.
    """
    by_type: dict[str, dict] = {}
    disagreements: list[dict] = []
    total = agree = 0

    for task_type, decision_fields in _DECISION_FIELDS.items():
        p_idx = _index_by_id((primary or {}).get(task_type))
        s_idx = _index_by_id((shadow or {}).get(task_type))
        ids = sorted(set(p_idx) | set(s_idx))
        t_total = t_agree = 0
        for task_id in ids:
            p, s = p_idx.get(task_id), s_idx.get(task_id)
            t_total += 1
            if p is not None and s is not None and all(
                _norm(f, p.get(f)) == _norm(f, s.get(f)) for f in decision_fields
            ):
                t_agree += 1
                continue
            disagreements.append({
                "type": task_type,
                "key": _entry_key(p or s, task_type),
                "primary": _slim(p, task_type) if p is not None else None,
                "shadow": _slim(s, task_type) if s is not None else None,
            })
        if t_total:
            by_type[task_type] = {"total": t_total, "agree": t_agree}
        total += t_total
        agree += t_agree

    return {
        "summary": {"total": total, "agree": agree, "by_type": by_type},
        "disagreements": disagreements,
    }


# ---------------------------------------------------------------------------
# Store lifecycle (shadow_runs.json)
# ---------------------------------------------------------------------------

def append_run(prior: dict, run: dict) -> dict:
    """Append this run's comparison record to the persisted store.

    ``prior`` is the parsed ``shadow_runs.json`` (``{}`` on first run or after
    a malformed read — the store is an experiment log, so starting fresh is
    always safe). Non-dict junk in ``runs`` is dropped in passing.
    """
    runs = [r for r in (prior or {}).get("runs") or [] if isinstance(r, dict)]
    runs.append(run)
    return {"runs": runs}


def _run_date(run: dict) -> date | None:
    raw = run.get("at")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def prune(store: dict, today: date | None = None) -> dict:
    """Drop runs older than ``_RETENTION_DAYS``; unparseable ``at`` is kept."""
    today = today or datetime.now(timezone.utc).date()
    kept: list[dict] = []
    for run in (store or {}).get("runs") or []:
        if not isinstance(run, dict):
            continue
        run_date = _run_date(run)
        if run_date is not None and (today - run_date).days > _RETENTION_DAYS:
            log.info("shadow_compare: pruning shadow run from %s", run.get("at"))
            continue
        kept.append(run)
    return {"runs": kept}


# ---------------------------------------------------------------------------
# Aggregation for the report CLI
# ---------------------------------------------------------------------------

def summarize(store: dict) -> dict:
    """Aggregate the whole store for ``src/shadow_report.py``.

    Returns run count, first/last timestamps, overall + per-type agreement,
    every disagreement (stamped with its run's ``at``), and summed usage for
    both models (for the cost comparison). Model names are taken from the most
    recent run (they don't change mid-experiment in practice).
    """
    runs = [r for r in (store or {}).get("runs") or [] if isinstance(r, dict)]
    total = agree = 0
    by_type: dict[str, dict] = {}
    disagreements: list[dict] = []
    primary_usage: dict[str, int] = {}
    shadow_usage: dict[str, int] = {}

    for run in runs:
        summary = run.get("summary") or {}
        total += summary.get("total") or 0
        agree += summary.get("agree") or 0
        for task_type, counts in (summary.get("by_type") or {}).items():
            slot = by_type.setdefault(task_type, {"total": 0, "agree": 0})
            slot["total"] += (counts or {}).get("total") or 0
            slot["agree"] += (counts or {}).get("agree") or 0
        for d in run.get("disagreements") or []:
            if isinstance(d, dict):
                disagreements.append({"at": run.get("at"), **d})
        for usage, slot in ((run.get("primary_usage"), primary_usage),
                            (run.get("shadow_usage"), shadow_usage)):
            for field, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    slot[field] = slot.get(field, 0) + value

    return {
        "runs": len(runs),
        "first_at": runs[0].get("at") if runs else None,
        "last_at": runs[-1].get("at") if runs else None,
        "primary_model": runs[-1].get("primary_model") if runs else None,
        "shadow_model": runs[-1].get("shadow_model") if runs else None,
        "total": total,
        "agree": agree,
        "by_type": by_type,
        "disagreements": disagreements,
        "primary_usage": primary_usage,
        "shadow_usage": shadow_usage,
    }
