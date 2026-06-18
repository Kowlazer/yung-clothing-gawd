# sale-check

Hybrid Python + Claude API daily sale checker for a clothing watchlist. Runs on GitHub Actions.

**Running it yourself?** See [SETUP.md](SETUP.md) for the full deployment guide
(creating the Gist/Doc, the Gmail app password, and the GitHub Actions secrets).
The quick local-only path is below.

## Local dev setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
cp .env.example .env          # then fill in real values
pytest
```

## Running locally

```
python -m src.main
```

## Browsing your wardrobe (local web app)

```
python -m src.wardrobe_browser
```

Opens a local browser at http://localhost:8787 to search and browse everything
you've bought (timeline, by category, by shop) — to avoid duplicate buys and
spot gaps. Read-only; only needs `GITHUB_TOKEN` + `GIST_ID`.
