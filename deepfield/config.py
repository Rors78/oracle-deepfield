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
# OPERATOR OVERRIDE: F10 cooldown OFF ("no blockers"). 0 makes should_alert()
# always true (now-last >= 0), so a confirmed BUY re-alerts AND re-places a live
# order on every daily/weekly close while it stays BUY — full pyramid/stacking on
# the same symbol (there is no separate dedupe; rails are also off). Set >0 to
# re-arm the per-symbol wait (e.g. 24 = the old once-a-day guard).
REALERT_HOURS = 0          # F10: per-symbol cooldown before re-alert (0 = disabled)
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
FLASH_SECS = 0.6           # tick-direction tint window; >= one render period at
                           # RENDER_HZ=2 so the flash is actually visible (spec's
                           # ~300ms would fall between frames half the time)

# --- Candle-close clock fallback (SPEC §5b) ---
# The WS ohlc feed sends NOTHING across an interval border until the next trade.
# The clock watchdog detects a forming bar past its deadline (+grace), REST-
# confirms the closed bar, flips it, and triggers the confirmed recompute.
CLOSE_GRACE_SECS = 5
CLOSE_POLL_SECS = 15

# --- REST throttle (Appendix B) ---
MIN_CALL_GAP = 0.6
FETCH_RETRIES = 2

# --- Telegram: env only, never in files, never committed (§10/§11) ---
TG_TOKEN = os.environ.get("ORACLE_TG_TOKEN")
TG_CHAT = os.environ.get("ORACLE_TG_CHAT")

# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION — live Kraken spot-margin (operator override, docs/RULINGS.md).
# Deterministic: signal fires -> size -> open leveraged long -> rest stop -> log.
# NO learning brain. Off by default; nothing can fire until EXEC_MODE flips.
# ═══════════════════════════════════════════════════════════════════════════
EXEC_MODE = os.environ.get("DEEPFIELD_EXEC_MODE", "off")   # off | paper | live

# Sizing. "min" (default, for now): buy the MINIMUM order per pair — positions so
# small nothing meaningful is ever at risk, so liquidation is a non-issue. "risk":
# 2% of equity off the stop (kept for later; revisit stop-vs-liquidation first).
EXEC_SIZE_MODE = os.environ.get("DEEPFIELD_EXEC_SIZE", "min")   # min | risk
RISK_PCT = 0.02
PAPER_PORTFOLIO_USD = 1000.0        # equity used for sizing math in paper/off

# Stop: weekly support (bottom-thesis invalidation), clamped to a sane band so a
# razor-thin stop can't blow up position size and a far one can't dust it.
STOP_MODE = "support"               # support | pct
STOP_PCT = 0.10                     # used when STOP_MODE="pct"
STOP_MIN_PCT = 0.05
STOP_MAX_PCT = 0.15
PROTECTIVE_STOP = True              # rest a real stop on the exchange (kill-safe)

ENTRY_ORDERTYPE = "limit"           # post-only maker ONLY (no market entries). A resting
                                    # limit is recorded status='pending' and promoted to
                                    # 'open' only when the fill monitor confirms it filled.
POST_ONLY_SLIP_PCT = 0.001          # bid this far BELOW last so the post-only maker can't
                                    # cross the ask (a crossing post-only is rejected -> silent
                                    # no-fill). 10bps ~= a patient bottom bid; negligible cost.
MARGIN_CAP_PCT = 0.90               # a single position may post at most this frac of free margin

# Risk rails (deterministic hard limits, from GoldenEye — NOT learners).
# OPERATOR OVERRIDE: automatic circuit breakers OFF ("no circuit breakers, no
# fear"). RAILS_ENABLED=False makes rails_ok skip the drawdown kill-switch, the
# daily/weekly loss caps, the max-positions gate, and the equity-unknown block —
# the bot never stops ITSELF. The manual HALT file (below) stays as the operator's
# hand-on-switch, and the per-position protective stop is the strategy's own exit,
# neither of which is a "circuit breaker". Flip True to re-arm the auto-brakes.
RAILS_ENABLED = False
MAX_OPEN_POSITIONS = 15
DAILY_LOSS_LIMIT_USD = 15.0         # halt new entries after this realized daily loss
WEEKLY_LOSS_LIMIT_USD = 35.0
KILL_SWITCH_DD_PCT = 0.20           # halt at 20% drawdown from peak equity (manual reset)
HALT_FILE = os.path.join(PROJECT_ROOT, "deepfield.HALT_ENTRIES")  # touch to halt / rm to resume

# Per-pair leverage — a FIXED hardcoded value Kraken must accept verbatim (it must
# be present in the pair's leverage_buy array). Sent exactly as-is on every order,
# hydra-style. Verified 2026-07-04 == max Kraken leverage_buy per pair.
PER_PAIR_LEVERAGE = {
    "BTC/USD": 10, "ETH/USD": 10, "XRP/USD": 10, "SOL/USD": 10, "DOGE/USD": 10,
    "ADA/USD": 10, "LINK/USD": 10, "SUI/USD": 10, "LTC/USD": 10, "AVAX/USD": 10,
    "AAVE/USD": 5, "UNI/USD": 5, "DOT/USD": 5, "BCH/USD": 5, "ALGO/USD": 2,
}
# Leveraged orders MUST use the :BTNL margin-book name (Non-ECP rejects spot name).
MARGIN_PAIR = {
    "BTC/USD": "XBTUSD:BTNL", "ETH/USD": "ETHUSD:BTNL", "XRP/USD": "XRPUSD:BTNL",
    "SOL/USD": "SOLUSD:BTNL", "DOGE/USD": "XDGUSD:BTNL", "ADA/USD": "ADAUSD:BTNL",
    "LINK/USD": "LINKUSD:BTNL", "SUI/USD": "SUIUSD:BTNL", "LTC/USD": "LTCUSD:BTNL",
    "AVAX/USD": "AVAXUSD:BTNL", "AAVE/USD": "AAVEUSD:BTNL", "UNI/USD": "UNIUSD:BTNL",
    "DOT/USD": "DOTUSD:BTNL", "BCH/USD": "BCHUSD:BTNL", "ALGO/USD": "ALGOUSD:BTNL",
}
# :BTNL margin-book PRICE precision (differs from spot — too many decimals rejects).
MARGIN_TICK_DECIMALS = {
    "BTC/USD": 1, "ETH/USD": 2, "XRP/USD": 5, "SOL/USD": 2, "DOGE/USD": 7,
    "ADA/USD": 6, "LINK/USD": 5, "SUI/USD": 4, "LTC/USD": 2, "AVAX/USD": 2,
    "AAVE/USD": 2, "UNI/USD": 3, "DOT/USD": 4, "BCH/USD": 2, "ALGO/USD": 5,
}
