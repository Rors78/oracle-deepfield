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
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("deepfield.broker")
_raw = logging.getLogger("deepfield.broker.raw")

BASE_URL = "https://api.kraken.com"
KEYFILES = [os.path.expanduser("~/.deepfield_keys"), os.path.expanduser("~/.hydra_keys")]
NONCE_FILE = os.path.expanduser("~/.deepfield_nonce")

_LAST_NONCE = 0
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
    n = int(time.time() * 1_000_000)
    if _LAST_NONCE == 0:
        try:
            p = int((open(NONCE_FILE).read().strip() or "0"))
            if n < p < n + 3_600_000_000:
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


def private(endpoint, params=None):
    """Signed POST to a Kraken private endpoint. Returns 'result' dict or None.
    Retries nonce/rate errors with a fresh higher nonce (hydra pattern)."""
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
            time.sleep(2)
    return None


def trade_balance():
    """Account equity in USD (TradeBalance 'e' = balance + unrealized net PnL),
    or None. This is the '2% of portfolio' denominator, live from Kraken."""
    r = private("/0/private/TradeBalance", {"asset": "ZUSD"})
    if not r:
        return None
    for k in ("e", "eb", "tb"):
        try:
            v = float(r.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def open_positions():
    """Open margin positions dict (or {}). Used for max-positions + orphan checks."""
    return private("/0/private/OpenPositions") or {}


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
