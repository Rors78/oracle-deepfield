"""In-memory published state — what the UI (M6) reads. SPEC §5, invariant 5.

The UI never re-implements a formula; it only reads ScoreCards and counters
published here by the writer/ingest task. This is a cache derived from DB reads
(invariant 3: state over events) — never a substitute for the DB.
"""
import time
from dataclasses import dataclass, field


@dataclass
class TrancheInfo:
    """F8 published result — UI reads this, never recomputes it (invariant 5)."""
    qty: float
    mult: float
    price: float           # the price actually used for the sizing calc
    price_is_live: bool    # True if from the tick; False if fallback to card.price


@dataclass
class PairState:
    symbol: str
    last_tick: object = None          # events.Tick or None
    last_tick_ts: float = 0.0         # wall-clock receipt time (F5 tick_age)
    flash_color: str = None           # "green"/"red" — last tick direction (§8)
    flash_until: float = 0.0          # monotonic deadline for the ~300ms tint
    confirmed: object = None          # engine.ScoreCard or None
    provisional: object = None        # engine.ScoreCard or None
    last_provisional_ts: float = 0.0  # monotonic; throttles F13 to <=1/s
    tranche: object = None            # TrancheInfo or None (F8, confirmed-only)
    cooldown_until: float = 0.0       # unix ts a confirmed BUY can re-fire (0 = clear)
    exec_plan: object = None          # dict: what live execution would place (display)
    # F10 cooldown is read from the alerts table on demand (store.last_alert_ts) —
    # disk is ground truth, no in-memory mirror to avoid drift after restart.

    def tick_age(self, now=None):
        now = time.time() if now is None else now
        return now - self.last_tick_ts if self.last_tick_ts else float("inf")


@dataclass
class AppState:
    pairs: dict = field(default_factory=dict)     # symbol -> PairState
    regime: object = None                          # engine.Regime or None
    reconnect_count: int = 0                       # legacy aggregate (kept for --once)
    recon_repairs: int = 0
    link_up: bool = False                          # legacy aggregate (all links up)
    links: dict = field(default_factory=dict)      # conn name -> {"up": bool, "reconnects": int}
    started_ts: float = field(default_factory=time.time)
    paused: bool = False                           # 'p' key freezes the render loop
    pause_dirty: bool = False                      # one render pending after toggle
    # Interval boundaries are shared across all 15 pairs (Kraken anchors every
    # symbol's daily/weekly bars to the same UTC calendar boundary) — one pair's
    # forming-bar interval_begin is enough to drive the countdown region (§8).
    daily_interval_begin: int = None
    weekly_interval_begin: int = None
    # Execution snapshot, published by the exec-refresh task (equity needs a
    # Kraken call, so it can't be render-time). UI reads this, never computes it.
    exec: dict = field(default_factory=lambda: {
        "mode": "off", "equity": None, "open_count": 0, "positions": [],
        "rails_ok": True, "rails_reason": "", "halt": False, "updated": 0.0,
    })

    def pair(self, symbol):
        if symbol not in self.pairs:
            self.pairs[symbol] = PairState(symbol=symbol)
        return self.pairs[symbol]

    def link_status(self):
        """Tri-state, per-connection honest: 'UP' (all), 'PARTIAL' (some),
        'DOWN' (none/no connections yet). A shared boolean lies when conn A is
        healthy and conn B is down — whichever event landed last would win."""
        if not self.links:
            return "DOWN"
        ups = [e.get("up", False) for e in self.links.values()]
        if all(ups):
            return "UP"
        if any(ups):
            return "PARTIAL"
        return "DOWN"

    def total_reconnects(self):
        return sum(e.get("reconnects", 0) for e in self.links.values())
