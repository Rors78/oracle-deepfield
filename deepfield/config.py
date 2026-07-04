"""Operator-edited CONFIG BLOCK (v4.4 'edit these freely' ethos). SPEC §10.

Runtime truth for ordermin/costmin/lot_decimals is the `pairs` table, refreshed
from AssetPairs at startup + daily. The numbers below are SEED/FALLBACK only —
never trusted as truth (SPEC §7 F8, Appendix C).
"""
import os

# Paths (single 916G root disk; project island under home). RULINGS env ruling.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_PKG_DIR)
DB_PATH = os.path.join(PROJECT_ROOT, "deepfield.db")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# Backfill/live intervals (minutes). SPEC §6.
INTERVALS = (1440, 10080)

# v1 asset code -> v2 symbol normalization (the rename traps). SPEC §6.
NORMALIZE = {"XBT": "BTC", "XDG": "DOGE"}

# --- Pairs (Appendix C). ordermin/costmin live-verified 2026-07-03. ---
# ws_symbol is derived from wsname via NORMALIZE at runtime; precomputed here.
PAIRS = [
    # rest,        wsname,      ws,          display, ordermin,  costmin
    {"rest": "XXBTZUSD", "wsname": "XBT/USD",  "ws": "BTC/USD",  "display": "BTC",  "ordermin": 0.00005, "costmin": 0.5},
    {"rest": "XETHZUSD", "wsname": "ETH/USD",  "ws": "ETH/USD",  "display": "ETH",  "ordermin": 0.001,   "costmin": 0.5},
    {"rest": "XXRPZUSD", "wsname": "XRP/USD",  "ws": "XRP/USD",  "display": "XRP",  "ordermin": 1.65,    "costmin": 0.5},
    {"rest": "SOLUSD",   "wsname": "SOL/USD",  "ws": "SOL/USD",  "display": "SOL",  "ordermin": 0.06,    "costmin": 0.5},
    {"rest": "SUIUSD",   "wsname": "SUI/USD",  "ws": "SUI/USD",  "display": "SUI",  "ordermin": 5,       "costmin": 0.5},
    {"rest": "XDGUSD",   "wsname": "XDG/USD",  "ws": "DOGE/USD", "display": "DOGE", "ordermin": 50,      "costmin": 0.5},
    {"rest": "XLTCZUSD", "wsname": "LTC/USD",  "ws": "LTC/USD",  "display": "LTC",  "ordermin": 0.1,     "costmin": 0.5},
    {"rest": "LINKUSD",  "wsname": "LINK/USD", "ws": "LINK/USD", "display": "LINK", "ordermin": 0.55,    "costmin": 0.5},
    {"rest": "ADAUSD",   "wsname": "ADA/USD",  "ws": "ADA/USD",  "display": "ADA",  "ordermin": 20,      "costmin": 0.5},
    {"rest": "AVAXUSD",  "wsname": "AVAX/USD", "ws": "AVAX/USD", "display": "AVAX", "ordermin": 0.5,     "costmin": 0.5},
    {"rest": "AAVEUSD",  "wsname": "AAVE/USD", "ws": "AAVE/USD", "display": "AAVE", "ordermin": 0.05,    "costmin": 0.5},
    {"rest": "UNIUSD",   "wsname": "UNI/USD",  "ws": "UNI/USD",  "display": "UNI",  "ordermin": 1.5,     "costmin": 0.5},
    {"rest": "DOTUSD",   "wsname": "DOT/USD",  "ws": "DOT/USD",  "display": "DOT",  "ordermin": 3.9,     "costmin": 0.5},
    {"rest": "BCHUSD",   "wsname": "BCH/USD",  "ws": "BCH/USD",  "display": "BCH",  "ordermin": 0.01,    "costmin": 0.5},
    {"rest": "ALGOUSD",  "wsname": "ALGO/USD", "ws": "ALGO/USD", "display": "ALGO", "ordermin": 41,      "costmin": 0.5},
]

# --- Scoring ---
MIN_RATIO = 5 / 7          # F3: required = max(2, round(MIN_RATIO * achievable))
STRICT_SEVEN = False       # True -> fixed 5-of-7 regardless of achievable
DOWN_WEEKS = 3             # F1: consecutive lower closes required before an up close
PIVOT_MIN_DEPTH = 0.015    # F2: divergence pivot prominence (1.5%)

# --- Freshness / regime ---
STALE_SECS = 180           # F5: tick_age beyond this -> STALE, alerts suppressed
DANGER_DRSI = 30           # §8: danger tag + tier boundary alignment

# --- Alerting ---
REALERT_HOURS = 24         # F10: per-symbol cooldown before re-alert
PROVISIONAL_ALERTS = False # invariant 7: provisional is display-only unless True

# --- Conviction multipliers (F8): score relative to required threshold ---
CONVICTION = {0: 1.0, 1: 1.5, 2: 2.0}  # +2 and above -> 2.0 (STARTER at 0)

# --- Named horizontal price levels (F7), display-only, operator-edited ---
LEVELS = {
    "BTC/USD": [("62.8k", 62858), ("57.6k", 57585)],
}

# --- UI cadence ---
SIMPLE_SECS = 60           # plaintext frame period in --simple mode
RENDER_HZ = 2              # rich Live render cap

# --- REST throttle (Appendix B) ---
MIN_CALL_GAP = 0.6
FETCH_RETRIES = 2

# --- Telegram: env only, never in files, never committed (§10/§11) ---
TG_TOKEN = os.environ.get("ORACLE_TG_TOKEN")
TG_CHAT = os.environ.get("ORACLE_TG_CHAT")
