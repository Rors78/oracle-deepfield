"""In-memory published state — what the UI (M6) reads. SPEC §5, invariant 5.

The UI never re-implements a formula; it only reads ScoreCards and counters
published here by the writer/ingest task. This is a cache derived from DB reads
(invariant 3: state over events) — never a substitute for the DB.
"""
import time
from dataclasses import dataclass, field


@dataclass
class PairState:
    symbol: str
    last_tick: object = None          # events.Tick or None
    last_tick_ts: float = 0.0         # wall-clock receipt time (F5 tick_age)
    confirmed: object = None          # engine.ScoreCard or None
    provisional: object = None        # engine.ScoreCard or None
    last_provisional_ts: float = 0.0  # monotonic; throttles F13 to <=1/s
    # F10 cooldown is read from the alerts table on demand (store.last_alert_ts) —
    # disk is ground truth, no in-memory mirror to avoid drift after restart.

    def tick_age(self, now=None):
        now = time.time() if now is None else now
        return now - self.last_tick_ts if self.last_tick_ts else float("inf")


@dataclass
class AppState:
    pairs: dict = field(default_factory=dict)     # symbol -> PairState
    regime: object = None                          # engine.Regime or None
    reconnect_count: int = 0
    recon_repairs: int = 0
    link_up: bool = False
    started_ts: float = field(default_factory=time.time)

    def pair(self, symbol):
        if symbol not in self.pairs:
            self.pairs[symbol] = PairState(symbol=symbol)
        return self.pairs[symbol]
