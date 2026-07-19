"""Operator-edited CONFIG BLOCK (v4.4 'edit these freely' ethos). SPEC §10.

Runtime truth for ordermin/costmin/lot_decimals is the `pairs` table, refreshed
from AssetPairs at startup + daily. The numbers below are SEED/FALLBACK only —
never trusted as truth (SPEC §7 F8, Appendix C).
"""
import os
import logging

_log = logging.getLogger(__name__)

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
    # 5x margin expansion (operator 2026-07-13 "add all of the 5x"). ordermin/costmin
    # from Kraken AssetPairs; each margin-enabled @ 5x, validated via --exec-probe.
    # NOT in SEED_PAIRS (5x pairs eat 2x the margin/notional — trade on confirmed BUYs
    # only, no auto-seeded chains). PAXG is a gold-backed token (tracks spot gold, not
    # a crypto cycle) — added then PULLED by operator 2026-07-13 (gold, not a cycle).
    # DROPPED — no :BTNL margin book on this account (EQuery:Unknown asset pair on the
    # validate probe, despite AssetPairs listing leverage_buy): BNB, FARTCOIN, TAO,
    # XAUT, XMR. Also HYPE: validates, but only ~25 weekly candles (2024 listing) — too
    # young for the weekly thesis, sits permanently NA, and tripped the parity gate.
    {"rest": "CRVUSD",   "wsname": "CRV/USD",  "ws": "CRV/USD",  "display": "CRV",  "ordermin": 20,      "costmin": 0.5},
    {"rest": "HBARUSD",  "wsname": "HBAR/USD", "ws": "HBAR/USD", "display": "HBAR", "ordermin": 55,      "costmin": 0.5},
    {"rest": "PEPEUSD",  "wsname": "PEPE/USD", "ws": "PEPE/USD", "display": "PEPE", "ordermin": 1500000, "costmin": 0.5},
    {"rest": "SHIBUSD",  "wsname": "SHIB/USD", "ws": "SHIB/USD", "display": "SHIB", "ordermin": 770000,  "costmin": 0.5},
    {"rest": "TRXUSD",   "wsname": "TRX/USD",  "ws": "TRX/USD",  "display": "TRX",  "ordermin": 16,      "costmin": 0.5},
    {"rest": "ZECUSD",   "wsname": "ZEC/USD",  "ws": "ZEC/USD",  "display": "ZEC",  "ordermin": 0.01,    "costmin": 0.5},
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
# COUPLING (audit F2): flipping this >0 re-arms the cooldown, but two strands must
# be fixed IN THE SAME change or it's porous exactly when it matters:
#  1. TOCTOU — the check (last_alert_ts) runs on the writer while the insert
#     (alerter.fire) runs in the offloaded dispatch thread, so two near-simultaneous
#     closes both pass. Make check+insert atomic on the writer.
#  2. The alerts table IS the cooldown ledger, but _dispatch now places the order
#     BEFORE alerter.fire and isolates the alert — so a failed alert records NO fire
#     for an order that DID happen, blinding the cooldown to it. When re-enabling,
#     record the fire on the ORDER path (not the decoration path), so the ledger
#     reflects orders placed, not alerts that happened to succeed.
PROVISIONAL_ALERTS = False # invariant 7: provisional is display-only unless True

# --- Conviction multipliers (F8): score relative to required threshold ---
CONVICTION = {0: 1.0, 1: 2.0, 2: 3.0}  # +1 -> 2x, +2 and above -> 3x (STARTER at 0)

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
# EXEC_MODE is fail-CLOSED: only the EXACT canonical strings arm anything. Any other
# value — a case slip ('LIVE'), a trailing space ('paper ', trivial in .env/systemd),
# or a safe-sounding typo ('test'/'sim'/'dry') — resolves to 'off' with a loud error.
# We deliberately do NOT lower()/strip()-coerce: coercing 'LIVE'->'live' would arm real
# money on a typo. Every downstream gate is a BLOCKLIST (mode=='off' returns early, else
# the code falls THROUGH to the live AddOrder path), and poll_fills/verify_open_stops are
# gated on exact 'live' — so an unrecognized mode that slipped past would place real
# leveraged orders that never get a protective stop or fill-reconcile. Catch it here.
_VALID_EXEC_MODES = ("off", "paper", "validate", "live")


def _normalize_exec_mode(raw):
    if raw in _VALID_EXEC_MODES:
        return raw
    _log.error("DEEPFIELD_EXEC_MODE=%r is not one of %s — refusing to arm; running OFF.",
               raw, _VALID_EXEC_MODES)
    return "off"


EXEC_MODE = _normalize_exec_mode(os.environ.get("DEEPFIELD_EXEC_MODE", "off"))   # off | paper | validate | live

# Web console — served in-process by the live bot (a daemon thread) so the one
# desktop launch brings up TUI + web together. Read-only; set DEEPFIELD_WEB=0 to
# disable. The desktop launcher opens the browser to this port.
WEB_ENABLED = os.environ.get("DEEPFIELD_WEB", "1") != "0"
WEB_PORT = int(os.environ.get("DEEPFIELD_WEB_PORT", "8787"))

# Sizing. "min" (default, for now): buy the MINIMUM order per pair — positions so
# small nothing meaningful is ever at risk, so liquidation is a non-issue. "risk":
# 2% of equity off the stop (kept for later; revisit stop-vs-liquidation first).
EXEC_SIZE_MODE = os.environ.get("DEEPFIELD_EXEC_SIZE", "min")   # min | risk
RISK_PCT = 0.02
PAPER_PORTFOLIO_USD = 1000.0        # equity used for sizing math in paper/off

# Per-order sanity ceiling (Finding 8): refuse any single order whose NOTIONAL
# (volume x entry — the leveraged position size, i.e. the blast radius) exceeds this.
# NOT a rail: it never halts the bot and never shrinks a valid min-size order (min
# notionals run ~$3-8). It converts "a corrupt `pairs` row or a flipped EXEC_SIZE_MODE
# silently changes the blast radius" into a refused order + a loud log — making
# min-sizing a CHECKED bound, not just a config knob. 0 = disabled; raise it if you
# deliberately move to larger risk-mode sizing.
EXEC_MAX_ORDER_NOTIONAL_USD = 50.0

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
ENTRY_TTL_SECS = 86400              # cancel a still-unfilled post-only entry bid after this
                                    # long (default 1 day) so stale bids don't pile up against
                                    # Kraken's open-order cap and crowd out protective stops
                                    # (Finding 5). Fills are unaffected (a filled bid is 'open',
                                    # not 'pending'), so stacking still works. 0 = never expire.
# Continuous laddering: when a resting entry FILLS, immediately drop the next rung one
# LADDER_STEP_PCT below the fill (post-only, min-fill, SAME support stop) so accumulation
# continues down toward the stop without waiting for a candle close or a restart. Bounded
# by a NATURAL FLOOR — a rung that would land at/under the stop is not placed — so a full
# descent is ~ (entry-stop)/step rungs (~8 at 1% over an ~8% stop), never a runaway. One
# resting rung per symbol at a time; a gap-down that puts the rung above market is rejected
# by post-only (ladder pauses, safe) until the next fill/close. LIVE mode only.
LADDER_CONTINUOUS = True
LADDER_STEP_PCT = 0.01              # next rung this far below the fill (1% ~= 8 rungs to an 8% stop)
LADDER_STOP_BUFFER = 0.0            # extra margin ABOVE the stop below which no rung is placed
MARGIN_CAP_PCT = 0.90               # a single position may post at most this frac of free margin

# Rung/entry size multiplier (operator 2026-07-13 "bigger rungs, stack as much as
# possible"): every min-mode order — confirmed-BUY entries, ladder rungs, seeds —
# is sized at SIZE_MULT x the min fill; conviction (2x/3x) stacks ON TOP, so a 3x-
# conviction rung at SIZE_MULT=3 is 9x min (~$30-45 notional — still under the
# conviction-scaled EXEC_MAX_ORDER_NOTIONAL ceiling). Fail-safe: an unparseable
# env override runs at 1x (min), never at a surprise size.
# 2026-07-15: default raised 3 -> 16 (operator "squeeze more profit"). At 1656%
# margin-level the book was ~6% deployed (~$115 notional on $181 equity); 16x
# targets margin-level ~350% (~$520 notional, still a 4.4x buffer over the 120%
# stack floor). Env still overrides. The ceiling scales with SIZE_MULT so no clamp.
try:
    SIZE_MULT = max(1.0, float(os.environ.get("DEEPFIELD_SIZE_MULT", "2")))  # 2026-07-19: 8->2. Operator T/P'd the heavy book (banked at $186) and reverted to the "old way" — 2x-minimum dip buys. 8 still built a book that twice took ML near a margin call; 2x keeps it light. (History: 16->8 on 07-16 after the $1,484/9x/ML-106% near-call.)
except ValueError:
    _log.error("DEEPFIELD_SIZE_MULT=%r is not a number — running at 1x (min size)",
               os.environ.get("DEEPFIELD_SIZE_MULT"))
    SIZE_MULT = 1.0

# Seeded chains (operator 2026-07-13 "open the ten 10:1 pairs"): every pair below
# keeps a ladder chain WORKING at all times — a pair with no open rows and no
# resting bid gets a post-only starter bid just below live, NOT gated on a
# confirmed BUY (the backtest showed the signal is beta; the ladder is the
# strategy). The 5x/2x pairs (AAVE/UNI/DOT/BCH/ALGO) are deliberately excluded —
# they eat 2-5x the margin per dollar of notional. Regime gate + HALT + all rung
# guards still apply. Empty tuple disables seeding.
SEED_PAIRS = ("BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "SUI/USD",
              "DOGE/USD", "LTC/USD", "LINK/USD", "ADA/USD", "AVAX/USD")

# Equity take-profit (operator 2026-07-13 "t/p out at +20%, then go again"): when
# live equity >= tp_baseline * (1 + TP_PCT), flatten the WHOLE book — cancel every
# resting bid and protective stop, market-close each pair's live open volume —
# then reset the baseline to post-flatten equity and let the seeder restack: a
# compounding stack->harvest->restack cycle. The closes are EVENT-TRIGGERED
# market orders sized to Kraken's OpenPositions volume at that instant — never a
# resting sell, so the no-resting-sell / net-short rule below stands intact.
TP_ENABLED = True
TP_PCT = 0.20

# Runtime exchange-truth sweep (audit 2026-07-13 #1): re-run the startup ledger↔Kraken
# reconcile (verify_open_stops) every this-many seconds from the poll cycle, so an
# intraday stop-fire, force-liquidation, or manual close is noticed within minutes —
# not at the next restart. Costs ~4 private API calls per pass at the current book.
# 0 disables (restores boot-only reconcile). NOT a rail: it only trues the ledger up
# to the exchange and cancels provably-orphaned stops; it never blocks an entry.
RUNTIME_RECON_SECS = 900

# Margin-level watch (audit 2026-07-13 #2). Kraken margin-calls at ml<=80% and force-
# liquidates from ml<=40% — bypassing every stop, invisibly to the ledger. This is
# protection against the EXCHANGE seizing the book, not a self-brake:
#  - below MARGIN_LEVEL_ALERT_PCT: fire a (throttled) safety alert. Display + noise only.
#  - below MARGIN_LEVEL_STACK_FLOOR_PCT: pause SEEDS and LADDER RUNGS only — confirmed-BUY
#    signal entries are NEVER gated by this (operator no-blockers stance). FAILS OPEN:
#    a stale/unknown margin level never pauses anything.
# 0 disables either threshold.
MARGIN_LEVEL_ALERT_PCT = 150
MARGIN_LEVEL_STACK_FLOOR_PCT = 160  # 2026-07-16: raised 120->160 after a market drop compressed ML to 106% (7% from trouble). Book operates at the floor, so this ~doubles the liquidation cushion.

# Defense buffer engine (audit Wave 1, DEEPFIELD_AUDIT_EVIDENCE.md §B/§G). The
# ml floor above is a ratio; the DEFENSE tiers below are the SAME comfort zone
# expressed in PRICE space — the adverse basket move (%) that would drop the
# account to Kraken's ml=40% force-liquidation line. With all pairs at 10:1,
# ml 160 ≡ exactly a 12% liq buffer, so NOMINAL=12 IS the ml-160 floor, just
# readable as "how far price can fall before liquidation" instead of a ratio.
# Tiers on buffer_liq_pct:  NOMINAL >= 12 > CAUTION >= 6 > CRITICAL.
# TELEMETRY/ALERTING ONLY — deepfield.defense never gates or sizes an order.
DEFENSE_BUFFER_NOMINAL_PCT = 12.0   # >= this liq buffer is healthy (== the ml-160 floor)
DEFENSE_BUFFER_CRITICAL_PCT = 6.0   # < this liq buffer pages an escalated 'liq-risk' alert

# ── Reverse gear (audit Wave 4): the DELEVERAGE GOVERNOR ─────────────────────
# The account had an accelerator (stacking) and a brake (the ml floor pausing new
# rungs) but NO reverse gear — when the book got too big for its cash, the only way
# out was price rising, stops firing, or the operator wiring a deposit (2026-07-16,
# at a 4.4% funding fee). This closes that gap: when the price-space liq buffer
# decays below TRIGGER, the bot sheds whole fill-lots (largest-notional first) until
# the buffer recovers to TARGET, then stops. The closes are EVENT-TRIGGERED market
# orders sized to live open long volume (capped at net long — can never flip short),
# exactly like the T/P flatten; NEVER a resting sell (long-only rule intact).
#
# Per-LOT by design (the 2026-07-16 out-of-band trim incident): it closes whole DB
# rows — cancel that lot's stop, market-close its volume, retire the row — so it can
# never oversize a stop (flip risk) or leave a partial position naked. It also cancels
# resting entry bids on fire, so a bid can't refill and re-lever mid-deleverage (the
# other failure seen that day). FAIL-OPEN: an unknown/None/flat buffer never trims.
# Capped per pass so a single bad read can't flatten the book.
#
# ARMED (operator-enabled 2026-07-16, after the near-margin-call + manual deleverage):
# this actuator autonomously SELLS to deleverage when the liq buffer decays below
# TRIGGER. Default ON so it survives a desktop relaunch; set DEEPFIELD_REVERSE_GEAR=0
# (env) to disable without a code edit. Dormant unless the buffer is actually < TRIGGER.
REVERSE_GEAR_ENABLED = os.environ.get("DEEPFIELD_REVERSE_GEAR", "1") != "0"
REVERSE_GEAR_TRIGGER_PCT = 8.0      # fire when liq buffer < this (lower CAUTION band, above CRITICAL 6)
REVERSE_GEAR_TARGET_PCT = 12.0      # trim until liq buffer >= this (== the 12% law / ml-160 floor)
REVERSE_GEAR_MAX_LOTS_PER_PASS = 4  # cap lots closed per poll pass; re-evaluates next cycle (bad read can't flatten)
REVERSE_GEAR_CANCEL_BIDS = True     # cancel resting entry bids on fire so they can't re-lever mid-trim

# Safety-alert channel (audit 2026-07-13 #3): sound + notify-send (+ Telegram iff the
# env vars are set) for money-path safety events — UNPROTECTED positions, reconcile
# mismatches, margin-level danger, T/P cycle events. Throttled per event-kind so a
# retry loop can't turn the speaker into a siren. 0 disables throttling (every event).
SAFETY_ALERT_THROTTLE_SECS = 1800

# Rollover-fee accounting (audit 2026-07-13 #2): Kraken charges 0.01-0.05% of notional
# per 4h to hold a margin position. Poll the Ledgers API for rollover/margin entries
# at this cadence and accumulate the paid fees into meta (fees_total / fees_day) so
# the drag is VISIBLE next to realized P&L instead of silently mimicking market losses.
# Display/accounting only — never gates an order. 0 disables the poll.
ROLLOVER_POLL_SECS = 3600

# FORK A regime gate: accumulate in weakness, not strength. When True, new confirmed
# entries AND ladder rungs are placed ONLY when the BTC regime is not confirmed BULL
# ("stop adding once BULL" — buy the fall/turn, not the strength). FAILS OPEN: an
# unknown/missing/other regime (BEAR/RECOVERY/NEUTRAL/UNKNOWN) still accumulates, so a
# stale or unavailable regime can never silently halt entries (operator no-blockers
# stance). Only the unambiguous BULL state pauses accumulation. Set False to disable.
ACCUMULATE_ONLY_IN_BEAR = True
NO_ACCUMULATE_REGIMES = ("BULL",)   # regimes that pause new entries/rungs when the gate is on
# NOTE: the strategy is LONG ONLY — a resting sell can net short (Kraken spot-margin has
# no reduce_only). The only sells are protective STOPS (sized to close a long) and the
# TP_ENABLED equity flatten above (event-triggered market closes sized to live open
# volume at that instant, operator-ordered 2026-07-13). Do NOT add resting sells.

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
# hydra-style. Verified 2026-07-04 == max Kraken leverage_buy per pair. HARDCODED TO
# THE PER-PAIR MAX ON PURPOSE — do NOT lower. (The fork-A 2x de-lever was a mistake and
# was reverted 2026-07-11 at operator direction.)
PER_PAIR_LEVERAGE = {
    "BTC/USD": 10, "ETH/USD": 10, "XRP/USD": 10, "SOL/USD": 10, "DOGE/USD": 10,
    "ADA/USD": 10, "LINK/USD": 10, "SUI/USD": 10, "LTC/USD": 10, "AVAX/USD": 10,
    "AAVE/USD": 5, "UNI/USD": 5, "DOT/USD": 5, "BCH/USD": 5, "ALGO/USD": 2,
    # 5x expansion (each is Kraken's per-pair max — the hard ceiling, never lower)
    "CRV/USD": 5, "HBAR/USD": 5, "PEPE/USD": 5,
    "SHIB/USD": 5, "TRX/USD": 5, "ZEC/USD": 5,
}
# Leveraged orders MUST use the :BTNL margin-book name (Non-ECP rejects spot name).
MARGIN_PAIR = {
    "BTC/USD": "XBTUSD:BTNL", "ETH/USD": "ETHUSD:BTNL", "XRP/USD": "XRPUSD:BTNL",
    "SOL/USD": "SOLUSD:BTNL", "DOGE/USD": "XDGUSD:BTNL", "ADA/USD": "ADAUSD:BTNL",
    "LINK/USD": "LINKUSD:BTNL", "SUI/USD": "SUIUSD:BTNL", "LTC/USD": "LTCUSD:BTNL",
    "AVAX/USD": "AVAXUSD:BTNL", "AAVE/USD": "AAVEUSD:BTNL", "UNI/USD": "UNIUSD:BTNL",
    "DOT/USD": "DOTUSD:BTNL", "BCH/USD": "BCHUSD:BTNL", "ALGO/USD": "ALGOUSD:BTNL",
    # 5x expansion — altname:BTNL, all validated by --exec-probe (real Kraken order-check)
    "CRV/USD": "CRVUSD:BTNL", "HBAR/USD": "HBARUSD:BTNL", "PEPE/USD": "PEPEUSD:BTNL",
    "SHIB/USD": "SHIBUSD:BTNL",
    "TRX/USD": "TRXUSD:BTNL", "ZEC/USD": "ZECUSD:BTNL",
}
# :BTNL margin-book PRICE precision (differs from spot — too many decimals rejects).
MARGIN_TICK_DECIMALS = {
    "BTC/USD": 1, "ETH/USD": 2, "XRP/USD": 5, "SOL/USD": 2, "DOGE/USD": 7,
    "ADA/USD": 6, "LINK/USD": 5, "SUI/USD": 4, "LTC/USD": 2, "AVAX/USD": 2,
    "AAVE/USD": 2, "UNI/USD": 3, "DOT/USD": 4, "BCH/USD": 2, "ALGO/USD": 5,
    # 5x expansion — :BTNL margin-book precision, confirmed by --exec-probe. CRV (4)
    # and SHIB (8) are LESS than their spot pair_decimals (5 and 9) — the margin book
    # is coarser, exactly the "differs from spot" case; probe rejected the spot value.
    "CRV/USD": 4, "HBAR/USD": 5, "PEPE/USD": 9,
    "SHIB/USD": 8, "TRX/USD": 6, "ZEC/USD": 2,
}
