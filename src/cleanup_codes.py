"""One-shot maintenance: re-filter codes.json through the current _is_valid_code.

Older runs of the harvest regex extracted marketing acronyms (SMS, STOP, OFF,
SALE, CODE, PROMO, USE, ...) and short noise tokens as if they were promo
codes. The fix in src/codes.py prevents NEW extractions but doesn't scrub the
historical entries already persisted in the Gist. This script reads the Gist,
filters codes.json with the current validity rule, prints the diff, and (with
--apply) writes the cleaned codes back. All other state files are preserved
verbatim.

Usage:
    python -m src.cleanup_codes              # dry-run, prints diff
    python -m src.cleanup_codes --apply      # commit the cleanup to the Gist

Reads the same env vars as the daily run (GIST_ID, GITHUB_TOKEN, ...).
"""
from __future__ import annotations

import sys

from src.codes import _is_valid_code
from src.config import load_config
from src.state import read_state, write_state


def _summarize(entry: dict | object) -> str:
    if not isinstance(entry, dict):
        return f"<non-dict entry: {entry!r}>"
    return (
        f"{entry.get('shop', '?')} / {entry.get('code', '?')} "
        f"(source={entry.get('source', '?')}, last_seen={entry.get('last_seen', '?')})"
    )


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass

    apply = "--apply" in sys.argv[1:]
    cfg = load_config()

    print("reading state from gist...")
    state = read_state(cfg.gist_id, cfg.github_token)
    before: list = list(state.get("codes") or [])

    kept: list = []
    dropped: list = []
    for entry in before:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        code = entry.get("code", "")
        if _is_valid_code(code):
            kept.append(entry)
        else:
            dropped.append(entry)

    print(f"\ncodes.json: {len(before)} entries before, "
          f"{len(kept)} after ({len(dropped)} would be dropped)\n")

    if dropped:
        print("Entries to drop:")
        for d in dropped:
            print(f"  - {_summarize(d)}")
    else:
        print("Nothing to clean up. Exiting.")
        return 0

    if not apply:
        print("\nDry run — no changes written. Re-run with --apply to commit.")
        return 0

    print("\nwriting cleaned codes.json back to gist...")
    # Pass every other state file through unchanged so write_state's optional
    # kwargs don't blank-out files we didn't intend to touch. _prune_prices /
    # _prune_codes still run on the way through, matching daily-run behavior.
    write_state(
        cfg.gist_id,
        cfg.github_token,
        prices=state.get("prices") or {},
        aliases=state.get("aliases") or {},
        codes=kept,
        fx=state.get("fx") or None,
        gmail=state.get("gmail") or None,
        voice=state.get("voice") or None,
        sms_aliases=state.get("sms_aliases") or None,
    )
    print(f"done. {len(dropped)} stale code entries removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
