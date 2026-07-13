"""Kraken REST — stdlib urllib. SPEC §6, Appendix B (throttle/retry VERBATIM).

Sync functions (Appendix B uses time.sleep); the async layer wraps calls in
asyncio.to_thread. Endpoints: OHLC (cap 720, row[0]=bar OPEN, freshness to CLOSE
per F5) and AssetPairs (wsname, ordermin, costmin, lot_decimals). UA = USER_AGENT (F11).
"""
import time
import json
import logging
import urllib.parse
import urllib.request

from . import USER_AGENT
from .config import MIN_CALL_GAP, FETCH_RETRIES

API = "https://api.kraken.com/0/public"

# --- Appendix B: proven throttle/retry, ported verbatim (adapted names) ---
_last_api_call = [0.0]


def _throttle():
    gap = time.time() - _last_api_call[0]
    if gap < MIN_CALL_GAP:
        time.sleep(MIN_CALL_GAP - gap)
    _last_api_call[0] = time.time()


def fetch_json(url, ua=USER_AGENT):
    """GET Kraken public endpoint with throttle + transient-aware retry.
    Returns parsed 'result' dict or None (after logging)."""
    raw = None
    for attempt in range(FETCH_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == FETCH_RETRIES:
                logging.exception(f"fetch network error {url}")
                return None
            time.sleep(2.0 * (attempt + 1))
            continue
        errs = raw.get("error") or []
        if errs:
            transient = any(("Too many" in str(e)) or ("Unavailable" in str(e))
                            or ("EService" in str(e)) for e in errs)
            if transient and attempt < FETCH_RETRIES:
                time.sleep(3.0 * (attempt + 1))
                continue
            logging.error(f"Kraken API error {url}: {errs}")
            return None
        break
    return raw.get("result") if raw else None


# --- Endpoint helpers ---

def fetch_assetpairs(rest_pairs):
    """One comma-joined AssetPairs call (§6). Returns Kraken's result dict
    (keyed by canonical pair id) or None."""
    arg = urllib.parse.quote(",".join(rest_pairs))
    return fetch_json(f"{API}/AssetPairs?pair={arg}")


def fetch_ticker(rest_pairs):
    """One comma-joined Ticker call. Returns Kraken's result dict (keyed by
    canonical pair id) or None. Each entry's c[0] is the last trade price."""
    arg = urllib.parse.quote(",".join(rest_pairs))
    return fetch_json(f"{API}/Ticker?pair={arg}")


def fetch_ohlc(rest_pair, interval, since=None):
    """Fetch OHLC candles for one pair/interval. Returns list of rows
    [time, open, high, low, close, vwap, volume, count] or None.
    Kraken caps the response at 720 rows regardless of `since`."""
    url = f"{API}/OHLC?pair={urllib.parse.quote(rest_pair)}&interval={interval}"
    if since is not None:
        url += f"&since={int(since)}"
    res = fetch_json(url)
    if not res:
        return None
    # result = {"<pairkey>": [[...], ...], "last": <ts>}; grab the candle array.
    for k, v in res.items():
        if k != "last" and isinstance(v, list):
            return v
    return None
