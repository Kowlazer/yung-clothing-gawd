# Sale-check web form (Google Apps Script)

One web app, two jobs, both reached from signed links in the daily digest email:

1. **Fit feedback** — a per-item fit-review form (or a "review all pending" list)
   that writes `fit_review` back into `wardrobe.json` **live** and audits to a
   Google Sheet. Links built by `src/fit_links.py`.
2. **Watchlist removal** — when an order scan matches a purchased item to a line
   on the watchlist Google Doc, the form shows the item + how it's listed and, on
   approval, **deletes that line from the Doc** (native `DocumentApp`), records
   the removal in `wardrobe.json` (`watchlist_exclusions` + `approved_for_removal`),
   and audits to the Sheet. Links built by `src/watchlist_links.py`.

This directory is **not** part of the Python package or the daily cron — it's
source for a standalone Apps Script web app you deploy once. The cron only
*builds signed links* to it; it never calls it.

## Files

| File | Role |
|---|---|
| `Code.gs` | Server: HMAC verify, Gist read-modify-write (raw_url/>1 MB aware), fit + removal submit handlers, Doc edit (`_removeDocLine`), Sheet append |
| `Form.html` | Client: fit form + review-all list + removal pages + message pages |
| `appsscript.json` | Manifest: V8, OAuth scopes (`script.external_request`, `spreadsheets`, `documents`), web-app access settings |

## Security model (read this)

The form **writes to your wardrobe and deletes Doc lines**, so links must be
unforgeable. Each fit link carries `?item=<id>&sig=<hmac>` where
`sig = HMAC_SHA256(FIT_LINK_SECRET, item_id)` as lowercase hex (review-all signs
`__all__`). Removal links use a **namespaced** message so a leaked fit link can't
trigger a deletion: `?remove=<id>&sig=<hmac of "remove:"+id>` (review-all signs
`remove-all`). `doGet`, `submitFitReview`, and `submitRemoval` all **verify the
signature before doing anything**. Access is `ANYONE_ANONYMOUS` (the link is
opened from email with no Google login), so the HMAC — not Google auth — is the
gate. The `GIST_TOKEN` lives only in Script Properties, server-side; it is never
sent to the browser. The Doc edit is defensive: `_removeDocLine` deletes **only**
on exact whole-line match, scoped below the `Shops and URLs:` header, and only
when exactly one line matches — otherwise it reports back and leaves the Doc
untouched (the buy is still recorded in `wardrobe.json`).

Keep `FIT_LINK_SECRET` **ASCII** (it's a random token — e.g. `openssl rand -hex 32`).
It must be byte-identical to the repo's `FIT_LINK_SECRET` Actions secret.

## One-time setup

### 1. Create the audit Sheet
Create a blank Google Sheet (e.g. "Sale-check fit reviews"). From its URL grab
the ID: `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit`. The
script creates a `Fit reviews` tab and a `Watchlist removals` tab with headers on
first submit.

### 2. Create the Apps Script project
Easiest is to paste:
1. Go to <https://script.google.com> → **New project**.
2. Replace the default `Code.gs` with this folder's `Code.gs`.
3. **+ → HTML** → name it `Form` → paste `Form.html`.
4. **Project Settings (gear) → "Show appsscript.json manifest file"**, then open
   `appsscript.json` in the editor and paste this folder's manifest.

(Or, with [clasp](https://github.com/google/clasp): `clasp create --type webapp`
in this directory and `clasp push` — the file names already match.)

### 3. Set Script Properties
**Project Settings → Script properties → Add script property**, five rows:

| Property | Value |
|---|---|
| `GIST_TOKEN` | the classic PAT with `gist` scope (same value as the repo's `GIST_TOKEN`) |
| `GIST_ID` | the state Gist id (repo's `GIST_ID`) |
| `FIT_LINK_SECRET` | your HMAC secret (also goes in the repo as an Actions secret) |
| `SHEET_ID` | the Sheet ID from step 1 |
| `WATCHLIST_DOC_ID` | the watchlist Google Doc id — the `<id>` in `https://docs.google.com/document/d/`**`<id>`**`/edit` (same Doc as the repo's `WATCHLIST_URL`). Required for the removal flow to edit the Doc |

### 4. Verify before deploying
In the editor, run **`selfTestSigning`** once (authorize when prompted) and check
the Execution log shows exactly:

```
item:        fb54d2df7928581d904b1bb5b1809e724c2e4a8692b5d8a722d96e3131a57532
all:         60c475ab051996f4385fdb8cc37baea944f7d7e845c69f15c2eb292f8c90f2b4
remove:item: 5e60aca5d4affc015d3b5b04254a2184054a5aeb7288fe5d2df131132157274c
remove-all:  14536c5ff4de7caa7b521234ab0e2383606e135dc4091ce067655b59f5c52ca6
```

These are the Python `fit_links.sign` / `watchlist_links` vectors over
`"test-secret"` — matching all four proves the GAS HMAC is byte-for-byte
compatible for both link families. Then run **`selfTestConfig`** to confirm all
five properties are set and the Gist + Sheet + Doc are reachable.

### 5. Deploy as a web app
**Deploy → New deployment → type: Web app**:
- **Execute as:** *Me* (so it runs with your Gist token + Sheet access).
- **Who has access:** *Anyone* (this is the anonymous level the email link needs).

Copy the **Web app URL** (ends in `/exec`). That's `FIT_FORM_BASE_URL`.

> Re-deploying: use **Deploy → Manage deployments → edit (pencil) → Version: New
> version** so the `/exec` URL stays the same. A brand-new deployment mints a new
> URL and you'd have to update the secret.
>
> **Upgrading an existing fit-only deployment to add removal:** after pasting the
> new `Code.gs`/`Form.html`/`appsscript.json`, the added `documents` scope makes
> Apps Script **re-prompt for authorization** the next time you run a function or
> open the web app — grant it (it runs as you, who own the Doc). Set the
> `WATCHLIST_DOC_ID` Script Property, run `selfTestConfig` to confirm the Doc is
> reachable, then push a **New version** deployment.

## Wire it into the cron (Phase 3)

Set both as **repo Actions secrets** (and local `.env`) so the digest can build
links:
- `FIT_FORM_BASE_URL` = the `/exec` URL
- `FIT_LINK_SECRET` = the same secret as the Script Property

Both link families (fit + removal) reuse these two secrets — there is **no
separate removal secret or URL**; the same `/exec` web app handles both via
different query params. The feature stays dormant until both are set. Optional
toggles: `FIT_FEEDBACK_DAILY`, `FIT_FEEDBACK_WEEKLY` (default on),
`FIT_FEEDBACK_WEEKLY_DAY` (default `fri`), and `WATCHLIST_REMOVAL_DAILY` (default
on — the "Bought — remove from watchlist?" digest section). See the secrets table
in `../CLAUDE.md`.

## How it stays consistent with the Python side

- **Signing contract:** `Code.gs` `_sign` ⇄ `src/fit_links.py` `sign` (HMAC-SHA256,
  lowercase hex; message = item id or `__all__`). Removal links mirror
  `src/watchlist_links.py`: message = `"remove:"+id` or `remove-all`; `_removalMessage`
  ⇄ `watchlist_links.removal_message`.
- **Doc edit + wardrobe record:** `submitRemoval` deletes the matched Doc line
  (`_removeDocLine`, exact-line match below `Shops and URLs:`) **and** appends a
  `watchlist_exclusions` row + sets `watchlist_match.approved_for_removal = true`
  — the same shape `order_scan._interactive_watchlist_approval` writes, so the
  daily cron's `_apply_wardrobe_exclusions` stops price-checking the line. Decline
  just sets `approved_for_removal = false`. The item itself is never removed from
  `wardrobe.items`, so the buy record survives the Doc deletion.
- **Schema:** the fields `submitFitReview` writes (`fit`, `areas`, `inseam_inches`,
  `next_time`, `verdict`, `notes`, `source:"web"`, `body_comp_summary`) match the
  `fit_review` schema documented in `src/order_scan.py`. Off-schema enum values
  are dropped server-side.
- **>1 MB Gist read:** `_fileJson` follows `raw_url` exactly like
  `state._file_content`. Without it `wardrobe.json` reads as `{}` and the next
  write would wipe it — do not remove that branch.
- **Concurrency:** `submitFitReview` re-reads the wardrobe immediately before
  writing, so a submission can't clobber a concurrent `order_scan` write with a
  stale copy.
- **Body-comp (review-time match):** on submit the form matches the cached DEXA
  scan in `body_scans.json` nearest `reviewed_at` and writes the full `body_comp`
  block (`matched_to:"fit_review"`) + `body_comp_summary` straight away, preserving
  any prior purchase-time block as `body_comp_at_purchase` — so a review is correct
  the instant it's left, with no `--backfill-bodycomp` step. `_matchScan` /
  `_buildBodyComp` mirror `bodyspec.nearest_result` / `body_comp_from_record`
  byte-for-byte; the 90-day gap is `BODY_SCAN_MAX_GAP_DAYS`. `body_scans.json` is
  refreshed ~weekly by the **daily cron** (`main._maybe_refresh_body_scans`, the
  only holder of BodySpec credentials); the web tier still holds none. When the
  cache is empty or no scan is within 90 days, it falls back to the item's
  existing `body_comp` summary.
