"""Render the shadow A/B experiment log into a swap/no-swap readout.

Cost lever #5 (issue #16): while ``SHADOW_MODEL`` is set, every daily fuzzy
call is also judged by the shadow model and the verdict diff accumulates in
``shadow_runs.json`` (see ``src/shadow_compare.py``). After ~a week of runs,
this command prints:

  * per-task-type and overall agreement rates,
  * a risk split of the sale-verdict disagreements — a shadow "yes" where the
    primary said no/unclear is the FALSE-POSITIVE class the user explicitly
    cares about most ("false positives waste attention"), so it's called out
    separately from missed-sale and other diffs,
  * every disagreement, dated, with both verdicts,
  * a cost comparison: what the recorded primary calls cost vs what the same
    traffic costs at the shadow model's prices.

Usage:
    python -m src.shadow_report

Reads the same env vars as the daily run (GIST_ID, GITHUB_TOKEN, ...). Pure
formatting lives in ``format_report`` so it's unit-testable without a Gist.
"""
from __future__ import annotations

from src.shadow_compare import summarize

# $ per 1M tokens (input, output), matched by substring on the model ID.
# Cache-write bills 1.25x input, cache-read 0.1x input (5-min ephemeral TTL).
# Source: Anthropic pricing as of 2026-07 — Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15,
# Opus 4.8 $5/$25. Estimates only; the Console usage dashboard is authoritative.
_PRICES = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (5.00, 25.00),
}


def _cost_usd(usage: dict | None, model: str | None) -> float | None:
    """Estimated $ cost of a summed usage dict at ``model``'s prices.

    None when the model isn't priced here or there's no usage recorded —
    the report prints "n/a" rather than a silently-wrong number.
    """
    if not usage or not model:
        return None
    prices = next(
        (p for key, p in _PRICES.items() if key in model.lower()), None,
    )
    if prices is None:
        return None
    in_price, out_price = prices
    return (
        (usage.get("input_tokens") or 0) * in_price
        + (usage.get("cache_creation_input_tokens") or 0) * in_price * 1.25
        + (usage.get("cache_read_input_tokens") or 0) * in_price * 0.10
        + (usage.get("output_tokens") or 0) * out_price
    ) / 1_000_000


def _pct(agree: int, total: int) -> str:
    return f"{100 * agree / total:.0f}%" if total else "—"


def _fmt_verdict(side: dict | None) -> str:
    """One disagreement side as compact text; '<missing>' when the model
    dropped the entry entirely (e.g. truncated tool-call JSON)."""
    if side is None:
        return "<missing>"
    parts: list[str] = []
    for field in ("status", "url", "matched_url", "starts_on", "ends_on",
                  "confidence"):
        if field in side and side.get(field) is not None:
            parts.append(f"{field}={side[field]}")
    desc = side.get("description")
    if desc:
        parts.append(f'"{desc}"')
    return " ".join(parts) or "<empty>"


def _risk_split(disagreements: list[dict]) -> tuple[int, int, int]:
    """(false_positive, missed_sale, other) counts over sale-verdict diffs.

    Only shop_sales/email_sales carry a yes/no/unclear status. A shadow "yes"
    the primary didn't give is the dangerous class for this project (false
    positives waste the user's attention — the rubric's own words); a shadow
    non-"yes" against a primary "yes" is a missed sale; everything else
    (no↔unclear wobble, date diffs on an agreed "yes", a missing side, and
    every resolutions/loose_matches diff) lands in "other".
    """
    false_pos = missed = other = 0
    for d in disagreements:
        primary, shadow = d.get("primary"), d.get("shadow")
        if (d.get("type") in ("shop_sales", "email_sales")
                and primary is not None and shadow is not None):
            p = (primary.get("status") or "").strip().lower()
            s = (shadow.get("status") or "").strip().lower()
            if s == "yes" and p != "yes":
                false_pos += 1
                continue
            if p == "yes" and s != "yes":
                missed += 1
                continue
        other += 1
    return false_pos, missed, other


def format_report(store: dict) -> str:
    """The full human-readable report for a parsed ``shadow_runs.json``."""
    agg = summarize(store)
    if not agg["runs"]:
        return (
            "No shadow runs recorded yet.\n"
            "Set SHADOW_MODEL (e.g. claude-haiku-4-5-20251001) in the daily "
            "workflow and let the cron run for ~a week."
        )

    lines: list[str] = []
    lines.append(
        f"Shadow A/B report — {agg['runs']} run(s), "
        f"{str(agg['first_at'])[:10]} → {str(agg['last_at'])[:10]}"
    )
    lines.append(
        f"Primary {agg['primary_model']}  vs  shadow {agg['shadow_model']}"
    )
    lines.append("")

    lines.append("Agreement:")
    for task_type in ("shop_sales", "email_sales", "resolutions",
                      "loose_matches"):
        counts = agg["by_type"].get(task_type)
        if not counts:
            continue
        lines.append(
            f"  {task_type:<14} {counts['agree']:>3}/{counts['total']:<3} "
            f"({_pct(counts['agree'], counts['total'])})"
        )
    lines.append(
        f"  {'overall':<14} {agg['agree']:>3}/{agg['total']:<3} "
        f"({_pct(agg['agree'], agg['total'])})"
    )
    lines.append("")

    disagreements = agg["disagreements"]
    if disagreements:
        false_pos, missed, other = _risk_split(disagreements)
        lines.append("Sale-verdict risk split:")
        lines.append(
            f"  shadow YES, primary NO/UNCLEAR (false-positive risk): {false_pos}"
        )
        lines.append(
            f"  shadow NO/UNCLEAR, primary YES (missed-sale risk):    {missed}"
        )
        lines.append(
            f"  other (wording/date/url/missing diffs):               {other}"
        )
        lines.append("")
        lines.append("Disagreements:")
        for d in disagreements:
            lines.append(
                f"  [{str(d.get('at'))[:10]}] {d.get('type')} "
                f"{d.get('key', '?')}:"
            )
            lines.append(f"      primary: {_fmt_verdict(d.get('primary'))}")
            lines.append(f"      shadow:  {_fmt_verdict(d.get('shadow'))}")
    else:
        lines.append("Disagreements: none — every compared verdict matched.")
    lines.append("")

    primary_cost = _cost_usd(agg["primary_usage"], agg["primary_model"])
    shadow_cost = _cost_usd(agg["shadow_usage"], agg["shadow_model"])
    lines.append("Cost (summed over recorded runs, estimated):")
    lines.append(
        f"  primary: {f'${primary_cost:.2f}' if primary_cost is not None else 'n/a'}"
        f"   shadow: {f'${shadow_cost:.2f}' if shadow_cost is not None else 'n/a'}"
    )
    if primary_cost and shadow_cost is not None:
        saving = 1 - shadow_cost / primary_cost
        lines.append(
            f"  swapping the fuzzy call to the shadow model would cut its "
            f"cost ~{100 * saving:.0f}%"
        )
    lines.append("")
    lines.append(
        "Swap guidance: safe when overall agreement is high AND the "
        "false-positive row is 0-ish over a full week — false positives are "
        "the failure mode this project optimises against. To swap: change "
        "DEFAULT_MODEL in src/claude_fuzzy.py and remove SHADOW_MODEL from "
        "the workflow. To abort: just remove SHADOW_MODEL."
    )
    return "\n".join(lines)


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass

    from src.config import load_config
    from src.state import read_state

    cfg = load_config()
    print("reading shadow_runs.json from gist...")
    state = read_state(cfg.gist_id, cfg.github_token)
    print()
    print(format_report(state.get("shadow_runs") or {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
