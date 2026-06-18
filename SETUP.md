# Setup & deployment

This guide walks through running **sale-check** for yourself — first locally, then
as the daily GitHub Actions cron. Everything is driven by your own credentials;
nothing is hardcoded, so a fresh fork only ever touches accounts *you* configure.

> **TL;DR:** create a few free accounts → put their values in a local `.env` →
> verify with a dry run → copy the same values into your fork's Actions secrets →
> enable Actions. The required set is **9 values**; everything else is optional.

---

## 1. What you'll need

| Service | Why | Cost |
|---|---|---|
| **GitHub account** | hosts the fork + Actions cron + the state Gist | free |
| **A secret GitHub Gist** | the app's only datastore (prices, wardrobe, codes…) | free |
| **A Google Doc** | your watchlist (shop names + product URLs) | free |
| **A Gmail account** | IMAP source for promo/code/sale-signal emails | free |
| **Resend** ([resend.com](https://resend.com)) | sends the digest email | free tier |
| **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com)) | the "fuzzy" Claude call (~$0.02–0.20/run) | pay-as-you-go |

Python **3.13+** is required.

---

## 2. Local setup

```bash
git clone https://github.com/<your-username>/<your-fork>.git
cd <your-fork>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # runtime + pytest; use `pip install -e .` for runtime only
cp .env.example .env             # then fill in real values (see §3–4)
pytest                           # ~500 tests, no .env needed — sanity check the install
```

Once `.env` is filled in, do a **dry run** — it does everything except the two
destructive side effects (writing the Gist and sending email):

```bash
SALE_CHECK_DRY_RUN=1 python -m src.main   # Windows: set it as an env var first
```

It writes the rendered digest to `digest.md` (gitignored) so you can inspect the
output. **Always use the dry-run flag for local runs** so you don't overwrite
production state or email yourself.

---

## 3. Create each resource

### a. State Gist → `GIST_ID`
GitHub won't create a truly empty Gist, so:
1. Go to [gist.github.com](https://gist.github.com), make a **secret** gist.
2. Filename `prices.json`, content `{}`. Create it.
3. Copy the ID from the URL (`https://gist.github.com/<you>/<THIS_PART>`).

The app creates the other state files (`wardrobe.json`, `codes.json`, …) on its
first write — you only seed this one.

### b. GitHub token → `GITHUB_TOKEN` (local) / `GIST_TOKEN` (Actions)
A **classic** Personal Access Token with the **`gist`** scope (read+write your
gists) at [github.com/settings/tokens](https://github.com/settings/tokens).
This is what lets the app read and update the state Gist.

> ⚠️ **Naming quirk:** in your local `.env` the key is **`GITHUB_TOKEN`**, but the
> GitHub Actions secret must be named **`GIST_TOKEN`**. Actions reserves
> `GITHUB_TOKEN` for an auto-injected token that lacks `gist` scope, so the
> workflow maps your `GIST_TOKEN` secret onto the `GITHUB_TOKEN` env var instead.

### c. Watchlist Google Doc → `WATCHLIST_URL`
1. Create a Google Doc.
2. Share it: **Anyone with the link → Viewer** (the app reads it unauthenticated
   via the plain-text export endpoint).
3. Paste the share URL as `WATCHLIST_URL` — the export URL is derived from it.

Minimal Doc format:

```
Shops and URLs:

Norse Projects:
https://www.norseprojects.com/products/some-chino

Acme Tees:
https://acmetees.example/products/cool-shirt
```

- Everything **above** a `Shops and URLs:` header is ignored (free scratch space).
- A `ShopName:` line starts a shop; product URLs go beneath it.
- Put `⭐` on a product-URL line to pin it to a "Watching now" block in the digest.
- Wrap notes in parentheses — `(runs small)` lines are ignored by the parser.
- Optional extra section headers: `Non-clothing Shops and URLs:` and
  `Shops to track sales for:` (see `.env.example` / the source for details).

### d. Gmail App Password → `GMAIL_USERNAME` + `GMAIL_APP_PASSWORD`
1. Enable **2-Step Verification** on the Gmail account.
2. Create an app password at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. `GMAIL_USERNAME` = the address; `GMAIL_APP_PASSWORD` = the 16-char password
   (paste with or without spaces — it's normalised).

App passwords are used for read-only IMAP (the Promotions tab + an optional
Google Voice label). Chosen over OAuth because OAuth refresh tokens for
unverified apps expire weekly.

### e. Resend → `RESEND_API_KEY` + `FROM_EMAIL` + `TO_EMAIL`
Sign up at [resend.com](https://resend.com), create an API key. For testing you
can send from `onboarding@resend.dev`; for real use, verify your own domain.
`TO_EMAIL` is wherever you want the digest delivered.

### f. Anthropic → `ANTHROPIC_API_KEY`
Create a key at [console.anthropic.com](https://console.anthropic.com). One
batched call per run; cost is dominated by homepage excerpts (~$0.02–0.20/day).

---

## 4. Required secrets (the 9)

These must all be set or the app aborts at startup with a clear `ConfigError`
(it never silently falls back to anything).

| `.env` key | Actions secret name | From |
|---|---|---|
| `WATCHLIST_URL` | `WATCHLIST_URL` | §3c |
| `RESEND_API_KEY` | `RESEND_API_KEY` | §3e |
| `FROM_EMAIL` | `FROM_EMAIL` | §3e |
| `TO_EMAIL` | `TO_EMAIL` | §3e |
| `GITHUB_TOKEN` | **`GIST_TOKEN`** | §3b (note the rename) |
| `GIST_ID` | `GIST_ID` | §3a |
| `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` | §3f |
| `GMAIL_USERNAME` | `GMAIL_USERNAME` | §3d |
| `GMAIL_APP_PASSWORD` | `GMAIL_APP_PASSWORD` | §3d |

---

## 5. Deploy the daily cron

1. Push your configured fork to GitHub (secrets stay in `.env`, which is
   gitignored — they are **not** committed).
2. In your fork: **Settings → Secrets and variables → Actions → New repository
   secret**, and add each value from the table above (remember `GIST_TOKEN`,
   not `GITHUB_TOKEN`).
3. **Enable Actions** — forks have workflows disabled by default. Open the
   **Actions** tab and click the button to enable them.
4. The **Daily sale check** workflow (`.github/workflows/daily.yml`) runs at
   **14:00 UTC** daily. To test immediately, open it in the Actions tab and use
   **Run workflow** (the `workflow_dispatch` trigger).

There's also an optional **Weekly order scan** workflow
(`.github/workflows/order-scan.yml`, Sundays 06:00 UTC) that scans Gmail for
order confirmations to build your wardrobe — it reuses the same secrets.

---

## 6. Optional features & their secrets

All optional — unset means the feature is dormant and the cron behaves as if it
weren't there. Add these as Actions secrets the same way (and, for local runs,
to `.env`).

**Size-aware stock detection**
- `PREFERRED_SIZES` — comma list, e.g. `M,L,XL`. Flags items out-of-stock in *your* sizes and shows the size matrix.
- `PREFERRED_SIZES_PANTS` — per-bottoms override (e.g. `S,M,L`).

**Google Voice SMS sale signals**
- `VOICE_GMAIL_LABEL` — the Gmail label your GV-forward filter applies (default `GoogleVoice`).
- `SMS_SALE_SHOPS` — comma list of non-watchlist shops you get marketing texts from (supplements the Doc's `Shops to track sales for:` section).

**Wardrobe / fit feedback web form** (Apps Script — see [apps_script/README.md](apps_script/README.md))
- `FIT_FORM_BASE_URL` — deployed Apps Script `/exec` URL.
- `FIT_LINK_SECRET` — HMAC secret shared with the script's Script Properties (signs per-item links).
- Toggles: `FIT_FEEDBACK_DAILY`, `FIT_FEEDBACK_WEEKLY`, `FIT_FEEDBACK_WEEKLY_DAY` (default `fri`), `WATCHLIST_REMOVAL_DAILY` — all default ON.

**BodySpec DEXA body-composition** (optional)
- `BODYSPEC_USERNAME`, `BODYSPEC_PASSWORD` — your app.bodyspec.com login.
- `BODY_SCAN_MAX_AGE_DAYS` — cache refresh age (default 7).

**Review-request & back-in-stock email surfacing** (default ON, reuse Gmail)
- `REVIEW_REQUESTS_DAILY` / `REVIEW_REQUESTS_DAYS` (default 30).
- `RESTOCK_EMAILS_DAILY` / `RESTOCK_EMAIL_DAYS` (default 7).

**Privacy**
- `EXCLUDED_SHOPS` — comma list of shops to keep out of the wardrobe entirely.

**Price-history / standing-discount tuning** (all default, rarely changed)
- `PRICE_HISTORY_RETENTION_DAYS` (365), `PRICE_BASELINE_DAYS` (90),
  `PRICE_HISTORY_MIN_DAYS` (7), `PRICE_DROP_MARGIN_PCT` (2.0),
  `VARIANT_HISTORY_RETENTION_DAYS` (365).

**Manual opt-in commands** (off by default, never run by the cron)
- `SIGNUP_ENABLED` + `SIGNUP_PHONE` — newsletter auto-signup (`python -m src.newsletter_signup`).
- `RESTOCK_SIGNUP_ENABLED` — back-in-stock signup (`python -m src.restock_signup`).
- Both use Playwright; run `playwright install chromium` first.

---

## 7. Troubleshooting

- **`ConfigError: missing required env vars`** — one of the 9 is unset/blank.
- **Run is green but no email** — check the Resend dashboard for the send event.
- **`malformed JSON in gist file …`** — a state file got corrupted; the Gist is
  the source of truth, inspect it directly via the API.
- **Cron didn't fire on time** — GitHub Actions cron can lag 5–15 min under load.

See `apps_script/README.md` for the optional web-form deployment, and the inline
comments in `.env.example` and the workflow files for per-variable detail.
