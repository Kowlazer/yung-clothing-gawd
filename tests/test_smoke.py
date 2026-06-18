"""Package-level smoke test — verifies key modules import cleanly."""

from src import classify, codes, config, digest, email_send, extract, fx
from src import main as main_mod
from src import sale_detect, state, watchlist
from src import claude_fuzzy


def test_modules_import():
    # If any module raises at import time (typo, missing dep, circular import),
    # the test fails before assertions even run.
    for mod in (
        classify, codes, config, digest, email_send, extract, fx,
        main_mod, sale_detect, state, watchlist, claude_fuzzy,
    ):
        assert mod.__name__.startswith("src.")


def test_main_has_run_and_main():
    assert callable(main_mod.run)
    assert callable(main_mod.main)
