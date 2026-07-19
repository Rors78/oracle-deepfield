"""Kraken private API — auth/nonce/signing ported from hydra `_kraken_private`.

Operator override (see docs/RULINGS.md): DEEPFIELD now places live margin orders.
This module is the signed-request layer only; order construction lives in
executor.py. Field-proven pattern from the operator's hydra.py.

Keys: two lines (key, then secret) in ~/.deepfield_keys, falling back to
~/.hydra_keys. **Use a DEDICATED Kraken API key for DEEPFIELD** — Kraken's nonce
is per-API-key, so DEEPFIELD and hydra sharing one key while both run would
collide nonces ("Invalid nonce"/"Invalid key"). Separate keys = separate nonce
sequences = no war.

Every private call is RAW-logged (nonce masked; key/sign live only in headers,
never logged) to logs/deepfield_orders_raw.log — the audit trail hydra taught.
"""
import os
import time
import json
import base64
import hashlib
import hmac
import logging
import threading
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("deepfield.broker")
_raw = logging.getLogger("deepfield.broker.raw")

BASE_URL = "https://api.kraken.com"
KEYFILES = [os.path.expanduser("~/.deepfield_keys"), os.path.expanduser("~/.hydra_keys")]
NONCE_FILE = os.path.expanduser("~/.deepfield_nonce")

_LAST_NONCE = 0
# _next_nonce is a read-modify-write on _LAST_NONCE + a write to NONCE_FILE, and
# private() runs concurrently from several threads (per-alert dispatch, poll_fills,
# equity refresh). Without this lock two threads can mint the SAME microsecond nonce
# -> one call rejected 'EAPI:Invalid nonce', and the file write can tear (Finding 7).
_NONCE_LOCK = threading.Lock()
# _NONCE_LOCK alone isn't enough: it serializes nonce MINTING, but the lock is freed
# before the HTTP send, so two threads can mint n and n+1 then have their requests
# ARRIVE at Kraken out of order (n+1 first) — Kraken rejects the later, lower nonce
# ('EAPI:Invalid nonce'). Kraken's nonce is strictly-increasing per key, so private
# calls simply cannot be concurrent: _SEND_LOCK makes mint→send atomic, so requests
# reach Kraken in the same monotonic order their nonces were minted.
_SEND_LOCK = threading.Lock()
_KEY = None
_SECRET = None
_KEY_SRC = None


def load_keys():
    """(key, secret, source_path) or (None, None, None). Cached after first hit."""
    global _KEY, _SECRET, _KEY_SRC
    if _KEY is not None:
        return _KEY, _SECRET, _KEY_SRC
    for path in KEYFILES:
        try:
            with open(path) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if len(lines) >= 2:
                _KEY, _SECRET, _KEY_SRC = lines[0], lines[1], path
                return _KEY, _SECRET, _KEY_SRC
        except Exception:
            continue
    return None, None, None


def keys_present():
    k, s, _ = load_keys()
    return bool(k and s)


def _next_nonce():
    """Strictly-increasing, restart-safe (hydra pattern): seed from
    max(clock, persisted+1), persist every call."""
    global _LAST_NONCE
    with _NONCE_LOCK:                       # serialize the whole RMW + file write
        n = int(time.time() * 1_000_000)
        if _LAST_NONCE == 0:
            try:
                p = int((open(NONCE_FILE).read().strip() or "0"))
                # Persisted high-water beats a stale/backward wall clock — NO upper cap.
                # A VM-snapshot restore / NTP step-back makes the clock < the last nonce
                # Kraken saw; the old 1h window skipped this and wedged ALL private calls
                # (Invalid nonce) while clobbering the good high-water. Always trust p.
                if p >= n:
                    n = p + 1
            except Exception:
                pass
        if n <= _LAST_NONCE:
            n = _LAST_NONCE + 1
        _LAST_NONCE = n
        try:
            with open(NONCE_FILE, "w") as f:
                f.write(str(n))
        except Exception:
            pass
        return str(n)


def sign(path, postdata, nonce, secret_b64):
    """Kraken API-Sign: base64(HMAC-SHA512(secret, path + SHA256(nonce+postdata)))."""
    secret = base64.b64decode(secret_b64)
    sha256 = hashlib.sha256((nonce + postdata).encode()).digest()
    return base64.b64encode(hmac.new(secret, path.encode() + sha256, hashlib.sha512).digest()).decode()


def private(endpoint, params=None, idempotent=True, meta=None):
    """Signed POST to a Kraken private endpoint. Returns 'result' dict or None.
    Retries nonce/rate ERRORS (from a received response — the request did NOT
    execute) with a fresh higher nonce. idempotent=False (AddOrder/CancelOrder):
    a NETWORK exception is NOT retried, because the order may already have landed
    and a blind resend would DUPLICATE it (a duplicate stop can open a short).

    meta (audit 2026-07-13): pass a dict to learn HOW a None happened —
    meta['definite']=True means Kraken RESPONDED (an error reject: the order did
    not execute); False means the network failed and the request MAY have landed.
    Callers that record 'rejected' must only do so on definite=True; an ambiguous
    None needs the userref recovery path, not a terminal status."""
    key, secret, _ = load_keys()
    if meta is not None:
        meta["definite"] = False
    if not key or not secret:
        log.error("no Kraken API keys (looked in %s) — cannot send %s", KEYFILES, endpoint)
        if meta is not None:
            meta["definite"] = True      # nothing was sent — definitely not on the book
        return None
    base = dict(params or {})
    url = BASE_URL + endpoint
    for attempt in range(5):
        wait = 0.0                              # backoff before the next attempt; 0 = give up
        # Hold _SEND_LOCK across mint→send so Kraken sees monotonic nonces (a request
        # can never overtake an earlier one with a lower nonce). Backoff sleeps happen
        # AFTER the lock is released, so a retry never stalls other callers.
        with _SEND_LOCK:
            p = dict(base)
            p["nonce"] = _next_nonce()
            postdata = urllib.parse.urlencode(p)
            sig = sign(endpoint, postdata, p["nonce"], secret)
            headers = {"API-Key": key, "API-Sign": sig,
                       "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "DEEPFIELD/1"}
            try:
                _raw.info("REQ %s %s", endpoint, postdata.replace(p["nonce"], "<nonce>"))
                req = urllib.request.Request(url, data=postdata.encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read()
                _raw.info("RESP %s %s", endpoint, raw.decode("utf-8", "replace"))
                data = json.loads(raw)
                err = data.get("error")
                if err:
                    es = str(err)
                    if ("Nonce" in es or "nonce" in es or "Invalid key" in es) and attempt < 4:
                        wait = 0.4
                    elif "Rate limit" in es and attempt < 4:
                        wait = 5.0
                    else:
                        log.error("private API error %s: %s", endpoint, es)
                        if meta is not None:
                            meta["definite"] = True   # Kraken responded: a real reject
                        return None
                else:
                    if meta is not None:
                        meta["definite"] = True
                    return data.get("result")
            except Exception as e:
                log.warning("private API attempt %d failed: %s", attempt + 1, e)
                if not idempotent:
                    log.error("%s not retried after network error — may or may not have "
                              "landed; caller must NOT blind-resend", endpoint)
                    return None
                wait = 2.0
        if wait:                                # lock released — back off, then retry
            time.sleep(wait)
            continue
        return None
    return None


def equity(balance):
    """Account equity in USD from a TradeBalance result: 'e' (balance + unrealized
    net PnL), falling back to 'eb'/'tb'; first >0 wins, else None. ONE definition
    so the dashboard, rails, peak, and the order path can never disagree."""
    if not balance:
        return None
    for k in ("e", "eb", "tb"):
        try:
            v = float(balance.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def trade_balance():
    """Live account equity in USD, or None."""
    return equity(private("/0/private/TradeBalance", {"asset": "ZUSD"}))


def open_positions():
    """Open margin positions dict, {} if none, or None on API FAILURE (callers must
    distinguish 'no positions' from 'could not check' — treating a failed check as
    'no positions' would abandon/mis-handle real open longs)."""
    return private("/0/private/OpenPositions")


def open_positions_calc():
    """OpenPositions WITH docalcs=true so each lot carries the mark-to-market `net`
    (unrealized P/L of the OPEN remainder, net of fees/rollover — vol_closed already
    excluded) and `value` fields. Without docalcs those come back "0". Used for the
    ground-truth open P/L shown on the dashboard, so it can never disagree with what
    Kraken itself reports. {} if flat, None on API failure. DISPLAY-ONLY caller."""
    return private("/0/private/OpenPositions", {"docalcs": "true"})


def open_orders():
    """All currently-resting orders as {txid: order_info}, {} if none, or None on API
    FAILURE. Kraken returns {'open': {txid: {...}}}; we hand back the inner map so a
    caller can find a stop that IS resting on the book but whose txid the ledger lost
    (a persist-race orphan) BEFORE blindly re-placing a duplicate. None (not {}) on
    failure so 'could not check' is never read as 'nothing resting'."""
    r = private("/0/private/OpenOrders")
    if r is None:
        return None
    return r.get("open") or {}


def cancel_order(txid):
    """Cancel an order by txid. Non-idempotent transport (no blind resend)."""
    if not txid:
        return None
    return private("/0/private/CancelOrder", {"txid": txid}, idempotent=False)


def cancel_order_batch(txids):
    """Cancel MANY orders by txid via CancelOrderBatch (50/call), replacing the
    account-wide CancelAll the T/P flatten used to fire (audit 2026-07-13 M4:
    CancelAll also swept manually-placed / other-system orders, and stripped the
    stop off any Kraken position the ledger had no row for — permanently naked).
    Targeted: only OUR txids are touched. Best-effort like CancelAll was — the
    flatten reconciles from definite post-sweep OpenOrders state either way.
    Returns the count Kraken reports canceled, or None if every chunk failed."""
    ids = [t for t in (txids or []) if t]
    if not ids:
        return 0
    total, any_ok = 0, False
    for i in range(0, len(ids), 50):
        # Kraken's form-encoded API wants the `orders` array as indexed keys
        # (orders[0]=A&orders[1]=B). A JSON-string blob OR repeated `orders=` keys
        # are both rejected 'EGeneral:Invalid arguments:orders' (the json.dumps form
        # never worked live — the T/P flatten could never sweep, looping forever).
        chunk = ids[i:i + 50]
        params = {f"orders[{j}]": t for j, t in enumerate(chunk)}
        r = private("/0/private/CancelOrderBatch", params, idempotent=False)
        if r is not None:
            any_ok = True
            try:
                total += int(r.get("count", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total if any_ok else None


def find_order_by_userref(userref):
    """Locate an order by our client userref — the recovery path for an AddOrder
    whose transport failed AMBIGUOUSLY (audit 2026-07-13 C3: without this, an
    order that actually landed becomes an untracked position with no stop).
    Checks resting orders first, then recently-closed ones. Returns
    (txid, order_dict) or (None, None) when definitely absent, or (None, 'unknown')
    when the API could not answer (caller must retry, not conclude)."""
    if not userref:
        return None, None
    oo = private("/0/private/OpenOrders", {"userref": str(userref)})
    if oo is None:
        return None, "unknown"
    for txid, od in (oo.get("open") or {}).items():
        return txid, od
    co = private("/0/private/ClosedOrders", {"userref": str(userref)})
    if co is None:
        return None, "unknown"
    for txid, od in (co.get("closed") or {}).items():
        return txid, od
    return None, None


# Seconds to wait between paginated Ledgers pages. Ledgers costs 2 counter points
# against a limit shared by EVERY private call the bot makes (a 55s sample on
# 2026-07-19 caught 273 of them — QueryOrders/TradeBalance/OpenPositions), so an
# unpaced walk is rejected almost immediately. Pacing keeps a SHORT walk alive; it
# is not enough to carry a long one, which is why callers must never start one.
LEDGERS_PAGE_PACE_SECS = 4.0

# Hard page ceiling for any single walk. 50 entries/page, so this bounds a walk to
# ~10 pages regardless of how much history matches — the counter is shared and a
# runaway walk starves the money path.
LEDGERS_MAX_PAGES = 10


def rollover_fees_since(start_ts):
    """Sum of margin rollover fees paid since `start_ts` (unix), from the Ledgers
    API (entry type 'rollover'; the charge is in the 'fee' field, asset ZUSD).
    Paginates via ofs (50/page, bounded by LEDGERS_MAX_PAGES). Returns
    (total_fee_usd, entry_count, newest_entry_ts, complete) or None on API failure.
    `complete` is False when the page ceiling cut the walk short, meaning the total
    is a floor, not the true sum — callers must not treat it as exact.
    Display/accounting only — never gates an order.

    Keep walks SHORT (fix 2026-07-19). This was called with start_ts=0 to seed an
    all-time total, on the assumption that ~20 pairs of 4h rollovers fit in a page
    budget. They do not: Kraken held 6011 matching entries (121 pages) and the walk
    died on the rate limit every single time, so the caller never banked its cursor
    and re-walked from 0 the next hour — fees_total sat at None from the day the
    feature was wired. The limit is shared with the bot's own private-API traffic,
    so no pacing rescues a walk that long. Pacing + the ceiling here bound the
    damage; the caller anchors its cursor forward so a long walk never starts."""
    total, count, newest = 0.0, 0, float(start_ts or 0)
    ofs = 0
    complete = True
    for page in range(LEDGERS_MAX_PAGES):
        if page:                       # pace only BETWEEN pages, never before the first
            time.sleep(LEDGERS_PAGE_PACE_SECS)
        r = private("/0/private/Ledgers",
                    {"type": "rollover", "start": str(start_ts or 0), "ofs": str(ofs)})
        if r is None:
            return None
        entries = r.get("ledger") or {}
        if not entries:
            break
        for e in entries.values():
            try:
                total += abs(float(e.get("fee", 0) or 0))
                newest = max(newest, float(e.get("time", 0) or 0))
                count += 1
            except (TypeError, ValueError):
                continue
        got = len(entries)
        ofs += got
        if got < 50:
            break
    else:
        # Fell out of the loop still on a full page: more matched than we read.
        complete = False
    return total, count, newest, complete


def trade_balance_full():
    """Full TradeBalance result dict (e=equity, m=margin used, mf=free margin,
    ml=margin level) or None."""
    return private("/0/private/TradeBalance", {"asset": "ZUSD"})


def query_orders(txids):
    """Batch order-info lookup: {txid: info_dict} for many txids in as few calls as
    possible (Kraken QueryOrders takes up to 50 comma-separated txids per request).
    A 60-position startup reconcile otherwise fires 60+ QueryOrders back-to-back and
    trips the private-API rate limit on every restart; this collapses it to ~2 calls.
    A txid ABSENT from the returned map means its status is UNKNOWN — callers must
    treat a missing key exactly like query_order's None (never 'definitely gone',
    which could trigger a blind stop re-place -> duplicate stop -> short). On API
    failure the chunk contributes nothing, so all its txids read as unknown."""
    ids = [t for t in (txids or []) if t]
    out = {}
    for i in range(0, len(ids), 50):                # Kraken cap: 50 txids per call
        r = private("/0/private/QueryOrders", {"txid": ",".join(ids[i:i + 50])})
        if r:
            out.update(r)
    return out


def query_order(txid):
    """Order info dict for a txid (has 'status': open|closed|canceled|...) or None.
    Single-txid case of query_orders (ONE definition, so both paths agree)."""
    if not txid:
        return None
    return query_orders([txid]).get(txid)


def setup_raw_log(log_dir):
    """Route the RAW order audit trail to its own file (append) — UNBOUNDED, no rotation.

    Operator prunes by hand when the drive fills; this bot owns the disk.
    """
    os.makedirs(log_dir, exist_ok=True)
    h = logging.FileHandler(os.path.join(log_dir, "deepfield_orders_raw.log"))
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _raw.handlers = [h]
    _raw.setLevel(logging.INFO)
    _raw.propagate = False
