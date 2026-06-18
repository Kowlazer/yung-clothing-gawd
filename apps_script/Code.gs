/**
 * Fit-feedback web form — server side.
 *
 * Serves a per-item fit-review form reached from a signed link in the
 * sale-check digest email, and writes the review straight back into
 * wardrobe.json on the state Gist (plus an audit row to a Google Sheet).
 *
 * Pairs with src/fit_links.py (link signing) and src/order_scan.py (schema +
 * the --backfill-bodycomp Phase B re-match). The HMAC verification here MUST
 * stay byte-for-byte compatible with fit_links.sign(): HMAC-SHA256(secret,
 * message) rendered as lowercase hex, message = item_id (per item) or the
 * literal "__all__" (review-all).
 *
 * Script Properties required (Project Settings → Script properties):
 *   GIST_TOKEN      classic PAT with `gist` scope (same as the cron's secret)
 *   GIST_ID         the state Gist id
 *   FIT_LINK_SECRET the shared HMAC secret (== repo's FIT_LINK_SECRET)
 *   SHEET_ID        a Google Sheet to append the audit log to
 */

var REVIEW_ALL_TOKEN = '__all__';
var WARDROBE_FILE = 'wardrobe.json';
var BODY_SCANS_FILE = 'body_scans.json';
var SHEET_TAB = 'Fit reviews';
var GITHUB_API = 'https://api.github.com/gists/';

// Watchlist-removal flow (see src/watchlist_links.py for the matching signing
// contract). Per-item message = 'remove:' + item_id; review-all = 'remove-all'.
// The 'remove:' prefix namespaces these away from the fit links (bare item id)
// so a leaked fit link can never trigger a Doc deletion and vice-versa.
var REMOVAL_ALL_TOKEN = 'remove-all';
var REMOVAL_SHEET_TAB = 'Watchlist removals';

// Header that begins the editable section of the watchlist Doc — deletions are
// scoped to paragraphs below it so a coincidental match in the Notes section
// above can't be removed. Compared case-insensitively after trimming.
var SHOPS_SECTION_HEADER = 'shops and urls:';

// Max days between a review and a DEXA scan for the scan to be attached — mirror
// of bodyspec.nearest_result's max_gap_days default (src/order_scan.py --max-gap-days).
var BODY_SCAN_MAX_GAP_DAYS = 90;

// Allowed enum values — mirror the schema in src/order_scan.py. Anything not in
// these sets is dropped server-side so a tampered POST can't write junk.
var FIT_VALUES = ['too_small', 'small', 'tts', 'large', 'too_large'];
var NEXT_TIME_VALUES = ['size_down', 'same', 'size_up', 'buy_again', 'avoid'];
var VERDICT_VALUES = ['keep', 'return', 'tailor'];
var AREA_KEYS = ['length', 'shoulders_chest', 'sleeves', 'sleeve_opening',
                 'waist_hips', 'inseam'];
var AREA_VALUES = ['short', 'good', 'long', 'tight', 'loose', 'wide'];


// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function _prop(name) {
  var v = PropertiesService.getScriptProperties().getProperty(name);
  if (!v) {
    throw new Error('Missing Script Property: ' + name +
                    ' (set it in Project Settings → Script properties)');
  }
  return v;
}


// ---------------------------------------------------------------------------
// HMAC (must match src/fit_links.sign)
// ---------------------------------------------------------------------------

function _toHex(signedBytes) {
  var out = '';
  for (var i = 0; i < signedBytes.length; i++) {
    var b = signedBytes[i];
    if (b < 0) { b += 256; }           // Apps Script bytes are signed (-128..127)
    var h = b.toString(16);
    if (h.length === 1) { h = '0' + h; }
    out += h;
  }
  return out;
}

function _sign(message, secret) {
  var bytes = Utilities.computeHmacSha256Signature(message, secret);
  return _toHex(bytes);
}

function _verify(message, sig, secret) {
  if (!sig || !secret) { return false; }
  var expected = _sign(message, secret);
  // Constant-time-ish compare: fixed iteration over equal-length strings.
  if (expected.length !== sig.length) { return false; }
  var diff = 0;
  for (var i = 0; i < expected.length; i++) {
    diff |= (expected.charCodeAt(i) ^ sig.charCodeAt(i));
  }
  return diff === 0;
}

// The signed message for a per-item removal link — mirrors
// src/watchlist_links.removal_message (bare id namespaced with the prefix).
function _removalMessage(itemId) {
  return 'remove:' + itemId;
}


// ---------------------------------------------------------------------------
// Gist read / write (raw_url-aware — wardrobe.json is >1 MB)
// ---------------------------------------------------------------------------

function _githubHeaders() {
  return {
    'Authorization': 'Bearer ' + _prop('GIST_TOKEN'),
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };
}

/** Fetch the whole Gist (one GET). Callers pull individual files via _fileJson. */
function _readGist() {
  var resp = UrlFetchApp.fetch(GITHUB_API + _prop('GIST_ID'), {
    method: 'get', headers: _githubHeaders(), muteHttpExceptions: true
  });
  if (resp.getResponseCode() >= 300) {
    throw new Error('Gist read failed: HTTP ' + resp.getResponseCode());
  }
  return JSON.parse(resp.getContentText());
}

/**
 * Parse one file out of a fetched Gist. GitHub truncates a file's inline
 * `content` once it exceeds 1 MB and exposes the full bytes at `raw_url`; we
 * follow that (with the bearer token — secret gists require it) exactly like
 * state._file_content, or wardrobe.json would read back as {} and the next
 * write would wipe it. Missing/empty file → `fallback`.
 */
function _fileJson(gist, name, fallback) {
  var f = (gist.files || {})[name];
  if (!f) { return fallback; }
  var content;
  if (f.truncated && f.raw_url) {
    var raw = UrlFetchApp.fetch(f.raw_url, {
      method: 'get',
      headers: { 'Authorization': 'Bearer ' + _prop('GIST_TOKEN') },
      muteHttpExceptions: true
    });
    if (raw.getResponseCode() >= 300) {
      throw new Error('Gist raw_url read failed for ' + name + ': HTTP ' +
                      raw.getResponseCode());
    }
    content = raw.getContentText();
  } else {
    content = f.content || '';
  }
  return content ? JSON.parse(content) : fallback;
}

function _normaliseWardrobe(wardrobe) {
  wardrobe = wardrobe || {};
  if (!wardrobe.items) { wardrobe.items = []; }
  if (!wardrobe.shop_fit_notes) { wardrobe.shop_fit_notes = {}; }
  if (!wardrobe.watchlist_exclusions) { wardrobe.watchlist_exclusions = []; }
  return wardrobe;
}

/** Cached BodySpec scan records from body_scans.json (or [] when absent). */
function _scansFrom(gist) {
  return (_fileJson(gist, BODY_SCANS_FILE, {}) || {}).scans || [];
}

/** Read just wardrobe.json (its own Gist GET). Used where scans aren't needed. */
function _readWardrobe() {
  return _normaliseWardrobe(_fileJson(_readGist(), WARDROBE_FILE, {}));
}

function _writeWardrobe(wardrobe) {
  var files = {};
  files[WARDROBE_FILE] = { content: JSON.stringify(wardrobe, null, 2) };
  var resp = UrlFetchApp.fetch(GITHUB_API + _prop('GIST_ID'), {
    method: 'patch',
    headers: _githubHeaders(),
    contentType: 'application/json',
    payload: JSON.stringify({ files: files }),
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() >= 300) {
    throw new Error('Gist write failed: HTTP ' + resp.getResponseCode() +
                    ' — ' + resp.getContentText());
  }
}

// JSON for inlining inside a <script> block: escape `<` so an item name
// containing "</script>" can't break out of the script context.
function _safeJson(obj) {
  return JSON.stringify(obj).replace(/</g, '\\u003c');
}

function _findItem(wardrobe, itemId) {
  var items = wardrobe.items || [];
  for (var i = 0; i < items.length; i++) {
    if (items[i].id === itemId) { return items[i]; }
  }
  return null;
}

function _isPending(item) {
  // Mirror fit_links.is_fit_pending.
  return (item.fit_review === null || item.fit_review === undefined) &&
         item.is_clothing !== false;
}

// Mirror watchlist_links.is_removal_pending: matched a Doc line, no decision yet.
function _isRemovalPending(item) {
  var m = item.watchlist_match;
  return !!m && (m.approved_for_removal === null ||
                 m.approved_for_removal === undefined);
}


// ---------------------------------------------------------------------------
// Body-comp matching (mirror of src/bodyspec.py — match the cached DEXA scan
// nearest the review date and shape it into the item's body_comp block)
// ---------------------------------------------------------------------------

// Parse an ISO date/datetime to UTC-midnight millis (date part only), so day
// math is TZ-independent. Mirrors bodyspec._to_date. Returns null if unparseable.
function _toDateMs(value) {
  if (!value) { return null; }
  var m = String(value).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) { return null; }
  return Date.UTC(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10));
}

function _isoDate(ms) {
  return ms === null ? null : new Date(ms).toISOString().slice(0, 10);
}

// Nearest scan record to targetISO within maxGapDays, else null. Mirrors
// bodyspec.nearest_result over the pre-shaped body_scans.json records.
function _matchScan(scans, targetISO, maxGapDays) {
  var target = _toDateMs(targetISO);
  if (target === null || !scans || !scans.length) { return null; }
  var best = null, bestGap = null;
  for (var i = 0; i < scans.length; i++) {
    var sd = _toDateMs(scans[i].scan_date || scans[i].start_time);
    if (sd === null) { continue; }
    var gap = Math.abs(Math.round((sd - target) / 86400000));
    if (bestGap === null || gap < bestGap) { bestGap = gap; best = scans[i]; }
  }
  if (best === null || bestGap > maxGapDays) { return null; }
  return best;
}

// Shape a cached scan record + this review's date into a body_comp block.
// Mirrors bodyspec.body_comp_from_record byte-for-byte (signed days_from_event:
// negative = scan predates the review).
function _buildBodyComp(rec, targetISO, matchedTo) {
  var scanMs = _toDateMs(rec.scan_date || rec.start_time);
  var targetMs = _toDateMs(targetISO);
  var days = (scanMs !== null && targetMs !== null)
    ? Math.round((scanMs - targetMs) / 86400000) : null;
  return {
    result_id: rec.result_id,
    scan_date: rec.scan_date || _isoDate(scanMs),
    matched_to: matchedTo,
    matched_date: _isoDate(targetMs),
    days_from_event: days,
    weight_kg: rec.weight_kg,
    body_fat_pct: rec.body_fat_pct,
    tissue_fat_pct: rec.tissue_fat_pct,
    lean_mass_kg: rec.lean_mass_kg,
    fat_mass_kg: rec.fat_mass_kg,
    bone_mass_kg: rec.bone_mass_kg,
    android_gynoid_ratio: rec.android_gynoid_ratio,
    regions: rec.regions || {},
    fetched_at: new Date().toISOString()
  };
}

function _summariseBodyComp(bc) {
  if (!bc) { return null; }
  return {
    weight_kg: bc.weight_kg,
    body_fat_pct: bc.body_fat_pct,
    lean_mass_kg: bc.lean_mass_kg,
    fat_mass_kg: bc.fat_mass_kg,
    scan_date: bc.scan_date,
    matched_to: bc.matched_to,
    matched_date: bc.matched_date,
    days_from_event: bc.days_from_event
  };
}


// ---------------------------------------------------------------------------
// GET — render the form or the review-all list
// ---------------------------------------------------------------------------

function doGet(e) {
  try {
    var p = (e && e.parameter) || {};
    var secret = _prop('FIT_LINK_SECRET');

    if (p.all === '1') {
      if (!_verify(REVIEW_ALL_TOKEN, p.sig, secret)) {
        return _message('Invalid link', 'This review-all link could not be verified.');
      }
      return _renderReviewAll(secret);
    }

    // Watchlist removal: review-all and per-item pages.
    if (p.removeall === '1') {
      if (!_verify(REMOVAL_ALL_TOKEN, p.sig, secret)) {
        return _message('Invalid link', 'This removal-review link could not be verified.');
      }
      return _renderRemovalAll(secret);
    }
    if (p.remove) {
      if (!_verify(_removalMessage(p.remove), p.sig, secret)) {
        return _message('Invalid link', 'This link could not be verified. ' +
          'It may be corrupted — open it straight from the email.');
      }
      var rgist = _readGist();
      var rwardrobe = _normaliseWardrobe(_fileJson(rgist, WARDROBE_FILE, {}));
      var ritem = _findItem(rwardrobe, p.remove);
      if (!ritem) {
        return _message('Not found', 'That item is no longer in your wardrobe.');
      }
      if (!ritem.watchlist_match) {
        return _message('Nothing to remove',
          'That item is not linked to any watchlist Doc line.');
      }
      return _renderRemovalForm(ritem, p.sig);
    }

    if (!p.item) {
      return _message('Missing link', 'No item was specified in this link.');
    }
    if (!_verify(p.item, p.sig, secret)) {
      return _message('Invalid link', 'This link could not be verified. ' +
        'It may be corrupted — open it straight from the email.');
    }

    var gist = _readGist();
    var wardrobe = _normaliseWardrobe(_fileJson(gist, WARDROBE_FILE, {}));
    var item = _findItem(wardrobe, p.item);
    if (!item) {
      return _message('Not found', 'That item is no longer in your wardrobe.');
    }
    return _renderForm(item, p.sig, wardrobe.shop_fit_notes || {}, _scansFrom(gist));
  } catch (err) {
    return _message('Error', String(err && err.message ? err.message : err));
  }
}

// ---------------------------------------------------------------------------
// POST — programmatic submit (used by the local wardrobe browser; the emailed
// form pages still submit via google.script.run). Body is JSON:
//   { action: "fit"|"removal", item, sig, ...fields }
// Dispatches to the SAME submit functions the form RPCs call, so the Gist
// write + audit Sheet + body-comp match all run through one implementation.
// Returns JSON: { ok: true, ... } or { ok: false, error: "..." }.
// ---------------------------------------------------------------------------

function doPost(e) {
  var out;
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var action = body.action;
    if (action === 'fit') {
      out = submitFitReview(body);
    } else if (action === 'removal') {
      out = submitRemoval(body);
    } else {
      out = { ok: false, error: 'unknown action: ' + action };
    }
  } catch (err) {
    out = { ok: false, error: String(err && err.message ? err.message : err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out))
    .setMimeType(ContentService.MimeType.JSON);
}


function _renderForm(item, sig, shopNotes, scans) {
  // Preview the body state that *will* be attached on submit: the cached scan
  // nearest today (the review date), not the stale purchase-time block. Falls
  // back to whatever body_comp the item already carries when no scan is in range.
  var todayISO = new Date().toISOString();
  var match = _matchScan(scans || [], todayISO, BODY_SCAN_MAX_GAP_DAYS);
  var preview = match
    ? _summariseBodyComp(_buildBodyComp(match, todayISO, 'fit_review'))
    : _summariseBodyComp(item.body_comp);
  var t = HtmlService.createTemplateFromFile('Form');
  t.mode = 'item';
  t.dataJson = _safeJson({
    item: {
      id: item.id,
      shop: item.shop || '',
      item_name: item.item_name || '(unnamed)',
      size: item.size || '',
      color: item.color || '',
      purchased_at: item.purchased_at || '',
      fit_review: item.fit_review || null,
      body_comp_summary: preview
    },
    sig: sig,
    shop_note: (item.shop && shopNotes[item.shop]) ? shopNotes[item.shop] : ''
  });
  return t.evaluate()
    .setTitle('Fit feedback')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function _renderReviewAll(secret) {
  var wardrobe = _readWardrobe();
  var base = ScriptApp.getService().getUrl();
  var pending = [];
  var items = wardrobe.items || [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    if (_isPending(it)) {
      pending.push({
        name: it.item_name || '(unnamed)',
        shop: it.shop || '',
        size: it.size || '',
        color: it.color || '',
        url: base + '?item=' + encodeURIComponent(it.id) +
             '&sig=' + encodeURIComponent(_sign(it.id, secret))
      });
    }
  }
  var t = HtmlService.createTemplateFromFile('Form');
  t.mode = 'all';
  t.dataJson = _safeJson({ items: pending });
  return t.evaluate()
    .setTitle('Fit feedback — review all')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

// -- Watchlist removal: single-item page ------------------------------------
function _renderRemovalForm(item, sig) {
  var match = item.watchlist_match || {};
  var t = HtmlService.createTemplateFromFile('Form');
  t.mode = 'remove';
  t.dataJson = _safeJson({
    item: {
      id: item.id,
      shop: item.shop || '',
      item_name: item.item_name || '(unnamed)',
      size: item.size || '',
      color: item.color || '',
      purchased_at: item.purchased_at || '',
      matched_line: match.matched_line || ''
    },
    sig: sig
  });
  return t.evaluate()
    .setTitle('Remove from watchlist')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

// -- Watchlist removal: review-all page (act on each row in place) -----------
function _renderRemovalAll(secret) {
  var wardrobe = _readWardrobe();
  var pending = [];
  var items = wardrobe.items || [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    if (_isRemovalPending(it)) {
      var match = it.watchlist_match || {};
      pending.push({
        id: it.id,
        name: it.item_name || '(unnamed)',
        shop: it.shop || '',
        size: it.size || '',
        color: it.color || '',
        matched_line: match.matched_line || '',
        // Per-row sig so the client can call submitRemoval without a fresh link.
        sig: _sign(_removalMessage(it.id), secret)
      });
    }
  }
  var t = HtmlService.createTemplateFromFile('Form');
  t.mode = 'removeall';
  t.dataJson = _safeJson({ items: pending });
  return t.evaluate()
    .setTitle('Remove from watchlist — review all')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function _message(title, body) {
  var t = HtmlService.createTemplateFromFile('Form');
  t.mode = 'message';
  t.dataJson = _safeJson({ title: title, body: body });
  return t.evaluate().setTitle(title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


// ---------------------------------------------------------------------------
// Submit — called from the client via google.script.run.submitFitReview
// ---------------------------------------------------------------------------

function submitFitReview(payload) {
  var secret = _prop('FIT_LINK_SECRET');
  if (!payload || !_verify(payload.item, payload.sig, secret)) {
    throw new Error('Could not verify this submission.');
  }
  if (FIT_VALUES.indexOf(payload.fit) === -1) {
    throw new Error('Pick an overall fit before submitting.');
  }

  // Read-modify-write: re-read so a concurrent order_scan/backfill write isn't
  // clobbered by a stale copy held while the form was open. One GET pulls both
  // the wardrobe and the cached DEXA scans.
  var gist = _readGist();
  var wardrobe = _normaliseWardrobe(_fileJson(gist, WARDROBE_FILE, {}));
  var scans = _scansFrom(gist);
  var item = _findItem(wardrobe, payload.item);
  if (!item) { throw new Error('That item is no longer in your wardrobe.'); }

  var review = {
    fit: payload.fit,
    reviewed_at: new Date().toISOString(),
    source: 'web'
  };

  // Optional per-area detail (drop anything off-schema).
  var areas = {};
  var anyArea = false;
  var inAreas = payload.areas || {};
  for (var k = 0; k < AREA_KEYS.length; k++) {
    var key = AREA_KEYS[k];
    var val = inAreas[key];
    if (val && AREA_VALUES.indexOf(val) !== -1) { areas[key] = val; anyArea = true; }
  }
  if (anyArea) { review.areas = areas; }

  if (payload.inseam_inches !== undefined && payload.inseam_inches !== null &&
      String(payload.inseam_inches).trim() !== '') {
    var inseam = parseFloat(payload.inseam_inches);
    if (!isNaN(inseam)) { review.inseam_inches = inseam; }
  }
  if (NEXT_TIME_VALUES.indexOf(payload.next_time) !== -1) {
    review.next_time = payload.next_time;
  }
  if (VERDICT_VALUES.indexOf(payload.verdict) !== -1) {
    review.verdict = payload.verdict;
  }
  if (payload.notes && String(payload.notes).trim()) {
    review.notes = String(payload.notes).trim();
  }

  // Attach the body state at the moment of review: match the cached DEXA scan
  // (body_scans.json, refreshed ~weekly by the cron) nearest reviewed_at. This
  // makes the review correct the instant it's left — no manual --backfill-bodycomp.
  // Keep-both: a prior purchase-time block is preserved as body_comp_at_purchase
  // (mirrors order_scan._run_bodycomp_backfill Phase B). When the cache is empty
  // or no scan is within range, fall back to the item's existing body_comp.
  var scan = _matchScan(scans, review.reviewed_at, BODY_SCAN_MAX_GAP_DAYS);
  if (scan) {
    var prior = item.body_comp;
    if (prior && prior.matched_to === 'purchase') {
      item.body_comp_at_purchase = prior;
    }
    item.body_comp = _buildBodyComp(scan, review.reviewed_at, 'fit_review');
    review.body_comp_summary = _summariseBodyComp(item.body_comp);
  } else {
    var fallback = _summariseBodyComp(item.body_comp);
    if (fallback) { review.body_comp_summary = fallback; }
  }

  item.fit_review = review;

  // Editable per-shop note.
  if (payload.shop_note !== undefined && item.shop) {
    if (!wardrobe.shop_fit_notes) { wardrobe.shop_fit_notes = {}; }
    var note = String(payload.shop_note).trim();
    if (note) {
      wardrobe.shop_fit_notes[item.shop] = note;
    } else {
      delete wardrobe.shop_fit_notes[item.shop];
    }
  }

  _writeWardrobe(wardrobe);
  _appendSheet(item, review);

  return { ok: true, item_name: item.item_name || '(unnamed)' };
}

function _appendSheet(item, review) {
  try {
    var ss = SpreadsheetApp.openById(_prop('SHEET_ID'));
    var sheet = ss.getSheetByName(SHEET_TAB);
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_TAB);
      sheet.appendRow([
        'reviewed_at', 'item_id', 'shop', 'item_name', 'size', 'color',
        'fit', 'next_time', 'verdict', 'inseam_inches', 'areas', 'notes'
      ]);
    }
    sheet.appendRow([
      review.reviewed_at, item.id, item.shop || '', item.item_name || '',
      item.size || '', item.color || '', review.fit,
      review.next_time || '', review.verdict || '',
      review.inseam_inches !== undefined ? review.inseam_inches : '',
      review.areas ? JSON.stringify(review.areas) : '',
      review.notes || ''
    ]);
  } catch (err) {
    // The Gist is the source of truth; a Sheet logging hiccup must not fail the
    // submission. Surface it in the execution log only.
    console.error('Sheet append failed: ' + err);
  }
}


// ---------------------------------------------------------------------------
// Watchlist removal — approve/decline + the native Doc edit
// ---------------------------------------------------------------------------

/**
 * Read one element's plain text, or null if it isn't a text-bearing block.
 * Paragraphs AND list items are both candidates (watchlist URLs may be bulleted).
 */
function _childText(child) {
  var type = child.getType();
  if (type === DocumentApp.ElementType.PARAGRAPH) { return child.asParagraph().getText(); }
  if (type === DocumentApp.ElementType.LIST_ITEM) { return child.asListItem().getText(); }
  return null;
}

/**
 * Delete the watchlist Doc line whose full text equals ``matchedLine``.
 *
 * Defensive by design — the wardrobe is the system of record, so a wrong delete
 * is worse than a missed one:
 *   - scoped to paragraphs BELOW the "Shops and URLs:" header (so a coincidental
 *     match in the Notes section above is never touched);
 *   - matches on exact trimmed equality of the whole block (never a substring,
 *     so a URL that's a prefix of another can't collide);
 *   - removes only when there's exactly one match. Zero → 'not_found';
 *     more than one → 'ambiguous' (left for manual cleanup).
 * Throws only on hard config errors (missing WATCHLIST_DOC_ID / unopenable Doc)
 * so the caller can abort before mutating the wardrobe and the user can retry.
 */
function _removeDocLine(matchedLine) {
  var docId = _prop('WATCHLIST_DOC_ID');
  var target = String(matchedLine || '').trim();
  if (!target) { return { status: 'not_found' }; }

  var body = DocumentApp.openById(docId).getBody();  // throws if unopenable
  var n = body.getNumChildren();

  var sectionStart = -1;
  for (var i = 0; i < n; i++) {
    var htxt = _childText(body.getChild(i));
    if (htxt !== null && htxt.trim().toLowerCase() === SHOPS_SECTION_HEADER) {
      sectionStart = i;
      break;
    }
  }
  var from = sectionStart >= 0 ? sectionStart + 1 : 0;

  var matches = [];
  for (var j = from; j < n; j++) {
    var t = _childText(body.getChild(j));
    if (t !== null && t.trim() === target) { matches.push(j); }
  }
  if (matches.length === 0) { return { status: 'not_found' }; }
  if (matches.length > 1) { return { status: 'ambiguous', count: matches.length }; }

  body.getChild(matches[0]).removeFromParent();
  return { status: 'removed' };
}

/**
 * Approve or decline a "remove from watchlist" candidate.
 *
 * Called from the client via google.script.run.submitRemoval. On **approve**:
 * delete the matched Doc line (best-effort, see _removeDocLine), flip
 * ``watchlist_match.approved_for_removal = true``, append a
 * ``watchlist_exclusions`` row (so the daily cron stops price-checking the line),
 * and audit-log. On **decline**: just flip the flag to ``false`` so the digest
 * stops re-surfacing it. Either way the item itself stays in the wardrobe — the
 * Doc becomes a disposable view; wardrobe.json is the durable record.
 */
function submitRemoval(payload) {
  var secret = _prop('FIT_LINK_SECRET');
  if (!payload || !_verify(_removalMessage(payload.item), payload.sig, secret)) {
    throw new Error('Could not verify this submission.');
  }
  var decision = payload.decision === 'decline' ? 'decline' : 'approve';

  // Read-modify-write: re-read so a concurrent order_scan/fit write isn't
  // clobbered by a stale copy held while the page was open.
  var wardrobe = _normaliseWardrobe(_fileJson(_readGist(), WARDROBE_FILE, {}));
  var item = _findItem(wardrobe, payload.item);
  if (!item) { throw new Error('That item is no longer in your wardrobe.'); }
  var match = item.watchlist_match;
  if (!match) { throw new Error('That item is not linked to a watchlist line.'); }
  var matchedLine = match.matched_line || '';

  if (decision === 'decline') {
    match.approved_for_removal = false;
    _writeWardrobe(wardrobe);
    _appendRemovalSheet(item, matchedLine, 'declined');
    return { ok: true, decision: 'decline', item_name: item.item_name || '(unnamed)' };
  }

  // Approve. Edit the Doc FIRST — a hard config error throws here, before we
  // mutate the wardrobe, so the user fixes config and retries cleanly. Soft
  // outcomes (not_found / ambiguous) still proceed: the buy is recorded and the
  // user is told the line needs a manual look.
  var docResult = _removeDocLine(matchedLine);

  match.approved_for_removal = true;
  var dup = false;
  for (var i = 0; i < wardrobe.watchlist_exclusions.length; i++) {
    if (wardrobe.watchlist_exclusions[i].item_id === item.id) { dup = true; break; }
  }
  if (!dup) {
    wardrobe.watchlist_exclusions.push({
      matched_line: matchedLine,
      added_at: new Date().toISOString(),
      item_id: item.id
    });
  }
  _writeWardrobe(wardrobe);
  _appendRemovalSheet(item, matchedLine, docResult.status);

  return {
    ok: true,
    decision: 'approve',
    item_name: item.item_name || '(unnamed)',
    doc_status: docResult.status,
    matched_line: matchedLine
  };
}

function _appendRemovalSheet(item, matchedLine, docStatus) {
  try {
    var ss = SpreadsheetApp.openById(_prop('SHEET_ID'));
    var sheet = ss.getSheetByName(REMOVAL_SHEET_TAB);
    if (!sheet) {
      sheet = ss.insertSheet(REMOVAL_SHEET_TAB);
      sheet.appendRow([
        'decided_at', 'item_id', 'shop', 'item_name', 'size', 'color',
        'matched_line', 'doc_status'
      ]);
    }
    sheet.appendRow([
      new Date().toISOString(), item.id, item.shop || '', item.item_name || '',
      item.size || '', item.color || '', matchedLine, docStatus
    ]);
  } catch (err) {
    console.error('Removal sheet append failed: ' + err);
  }
}


// ---------------------------------------------------------------------------
// Deploy-time checks (run manually from the editor; not part of the web app)
// ---------------------------------------------------------------------------

/**
 * Confirm this runtime's HMAC matches src/fit_links.sign + src/watchlist_links
 * byte-for-byte. Run from the editor, then compare the logged values to these
 * reference vectors (secret 'test-secret'):
 *   item        -> fb54d2df7928581d904b1bb5b1809e724c2e4a8692b5d8a722d96e3131a57532
 *   all         -> 60c475ab051996f4385fdb8cc37baea944f7d7e845c69f15c2eb292f8c90f2b4
 *   remove:item -> 5e60aca5d4affc015d3b5b04254a2184054a5aeb7288fe5d2df131132157274c
 *   remove-all  -> 14536c5ff4de7caa7b521234ab0e2383606e135dc4091ce067655b59f5c52ca6
 */
function selfTestSigning() {
  Logger.log('item:        ' + _sign('a1b2c3d4e5f6', 'test-secret'));
  Logger.log('all:         ' + _sign(REVIEW_ALL_TOKEN, 'test-secret'));
  Logger.log('remove:item: ' + _sign(_removalMessage('a1b2c3d4e5f6'), 'test-secret'));
  Logger.log('remove-all:  ' + _sign(REMOVAL_ALL_TOKEN, 'test-secret'));
}

/** Confirm Script Properties are set and the Gist + Sheet + Doc are reachable. */
function selfTestConfig() {
  ['GIST_TOKEN', 'GIST_ID', 'FIT_LINK_SECRET', 'SHEET_ID', 'WATCHLIST_DOC_ID']
    .forEach(function (k) {
      Logger.log(k + ': ' + (PropertiesService.getScriptProperties().getProperty(k) ? 'set' : 'MISSING'));
    });
  var w = _readWardrobe();
  Logger.log('wardrobe items: ' + (w.items ? w.items.length : 0));
  SpreadsheetApp.openById(_prop('SHEET_ID')).getName();  // throws if unreachable
  Logger.log('sheet: reachable');
  var doc = DocumentApp.openById(_prop('WATCHLIST_DOC_ID'));  // throws if unreachable
  Logger.log('doc: reachable (' + doc.getName() + ')');
}
