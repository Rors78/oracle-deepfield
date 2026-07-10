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


def private(endpoint, params=None, idempotent=True):
    """Signed POST to a Kraken private endpoint. Returns 'result' dict or None.
    Retries nonce/rate ERRORS (from a received response — the request did NOT
    execute) with a fresh higher nonce. idempotent=False (AddOrder/CancelOrder):
    a NETWORK exception is NOT retried, because the order may already have landed
    and a blind resend would DUPLICATE it (a duplicate stop can open a short)."""
    key, secret, _ = load_keys()
    if not key or not secret:
        log.error("no Kraken API keys (looked in %s) — cannot send %s", KEYFILES, endpoint)
        return None
    base = dict(params or {})
    url = BASE_URL + endpoint
    for attempt in range(5):
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
                    time.sleep(0.4)
                    continue
                if "Rate limit" in es and attempt < 4:
                    time.sleep(5)
                    continue
                log.error("private API error %s: %s", endpoint, es)
                return None
            return data.get("result")
        except Exception as e:
            log.warning("private API attempt %d failed: %s", attempt + 1, e)
            if not idempotent:
                log.error("%s not retried after network error — may or may not have "
                          "landed; caller must NOT blind-resend", endpoint)
                return None
            time.sleep(2)
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


def cancel_order(txid):
    """Cancel an order by txid. Non-idempotent transport (no blind resend)."""
    if not txid:
        return None
    return private("/0/private/CancelOrder", {"txid": txid}, idempotent=False)


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
    """Route the RAW order audit trail to its own file (append), 5MB x 3."""
    from logging.handlers import RotatingFileHandler
    os.makedirs(log_dir, exist_ok=True)
    h = RotatingFileHandler(os.path.join(log_dir, "deepfield_orders_raw.log"),
                            maxBytes=5 * 1024 * 1024, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _raw.handlers = [h]
    _raw.setLevel(logging.INFO)
    _raw.propagate = False
