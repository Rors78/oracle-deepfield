"""Single writer: consumes the event queue, persists to DB, updates published
state, triggers confirmed/provisional recompute, and gates the alert chain via
F10 cooldown. SPEC §5, invariants 1-3, F5/F10/F13.

The stream is transport, not truth (invariant 3): both confirmed and
provisional recompute re-read the closed/forming series from the DB rather
than accumulating a series in memory from events.
"""
import time
import logging

from . import store
from . import engine
from . import events
from . import alerter
from . import config
from .profiles import FULL
from .state import TrancheInfo
from .config import REALERT_HOURS, PROVISIONAL_ALERTS, STALE_SECS

log = logging.getLogger("deepfield.ingest")

BTC_SYMBOL = "BTC/USD"


def _elapsed_fraction(interval_begin, interval_min, now=None):
    now = time.time() if now is None else now
    span = interval_min * 60
    return max(0.0, min(1.0, (now - interval_begin) / span))


class Ingest:
    def __init__(self, conn, appstate, profile=FULL):
        self.conn = conn
        self.state = appstate
        self.profile = profile
        self._pair_info_cache = {}  # symbol -> dict|None; ordermin/costmin/lot_decimals
                                     # are static enough (AssetPairs refresh is daily,
                                     # §10) that per-tick recompute must not hit the DB.

    # ── event handlers ──────────────────────────────────────────────────────

    def handle_tick(self, ev: events.Tick):
        ps = self.state.pair(ev.symbol)
        if ps.last_tick is not None and ev.last != ps.last_tick.last:
            ps.flash_color = "green" if ev.last > ps.last_tick.last else "red"
            ps.flash_until = time.monotonic() + 0.3
        ps.last_tick = ev
        ps.last_tick_ts = ev.ts
        # Keep the champion card's tranche priced off the live tick, not stale
        # from the last close/startup-sweep — found via the M6 export proof,
        # where "Live entry" and "Tranche" showed two different prices.
        if ps.confirmed is not None:
            self._compute_tranche(ev.symbol, ps.confirmed)

    def handle_candle_update(self, ev: events.CandleUpdate):
        closed = 1 if time.time() >= ev.interval_begin + ev.interval * 60 else 0
        store.upsert_candle(self.conn, ev.symbol, ev.interval, ev.interval_begin,
                             ev.o, ev.h, ev.l, ev.c, ev.v, closed)
        self.conn.commit()
        # Interval boundaries are shared across all 15 pairs — any pair's
        # forming-bar interval_begin drives the UI countdown region (§8).
        if ev.interval == 1440:
            self.state.daily_interval_begin = ev.interval_begin
        elif ev.interval == 10080:
            self.state.weekly_interval_begin = ev.interval_begin
        self._maybe_recompute_provisional(ev.symbol)

    def handle_candle_closed(self, ev: events.CandleClosed):
        n = store.flip_closed(self.conn, ev.symbol, ev.interval, ev.interval_begin)
        self.conn.commit()
        if n == 0:
            row = self.conn.execute(
                "SELECT closed FROM candles WHERE pair=? AND interval=? AND ts=?",
                (ev.symbol, ev.interval, ev.interval_begin),
            ).fetchone()
            if row is None:
                log.warning("CandleClosed for a row not in DB yet: %s/%s ts=%d (reconciler will gap-heal)",
                            ev.symbol, ev.interval, ev.interval_begin)
            else:
                log.debug("CandleClosed duplicate (already closed): %s/%s ts=%d",
                          ev.symbol, ev.interval, ev.interval_begin)
        self._recompute_confirmed(ev.symbol)
        if ev.symbol == BTC_SYMBOL:
            self._recompute_regime()

    def handle_link_up(self, ev: events.LinkUp):
        self.state.link_up = True
        self.state.reconnect_count = ev.reconnect_count

    def handle_link_down(self, ev: events.LinkDown):
        self.state.link_up = False

    def handle_recon_repair(self, ev: events.ReconRepair):
        self.state.recon_repairs += 1

    async def run(self, queue):
        dispatch = {
            events.Tick: self.handle_tick,
            events.CandleUpdate: self.handle_candle_update,
            events.CandleClosed: self.handle_candle_closed,
            events.LinkUp: self.handle_link_up,
            events.LinkDown: self.handle_link_down,
            events.ReconRepair: self.handle_recon_repair,
        }
        while True:
            ev = await queue.get()
            handler = dispatch.get(type(ev))
            if handler is not None:
                handler(ev)

    # ── startup ──────────────────────────────────────────────────────────────

    def startup_sweep(self):
        """Populate confirmed ScoreCards + regime from the DB's closed series
        right after warm-backfill — otherwise the TUI launches blank and stays
        blank until the next real candle close (up to 7 days for weekly).
        Also used by `--once`. No alerts fire here: this is not a live
        transition, just publishing already-true state (F10 cooldown is keyed
        off real confirmed-BUY *events*, and a fresh process start isn't one)."""
        for p in config.PAIRS:
            symbol = p["ws"]
            weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
            card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
            self.state.pair(symbol).confirmed = card
            self._compute_tranche(symbol, card)
        self._recompute_regime()

    # ── recompute + alert gating ────────────────────────────────────────────

    def _recompute_confirmed(self, symbol):
        weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
        card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
        self.state.pair(symbol).confirmed = card
        self._compute_tranche(symbol, card)
        if card.status == "BUY":
            ps = self.state.pair(symbol)
            if engine.is_stale(ps.tick_age(), STALE_SECS):
                log.info("F5: suppressing confirmed alert for %s — STALE (tick_age=%.0fs > %ds)",
                         symbol, ps.tick_age(), STALE_SECS)
            else:
                self._maybe_alert(symbol, card, kind="confirmed")
        return card

    def _recompute_regime(self):
        weekly, daily = store.load_weekly_daily_closed(self.conn, BTC_SYMBOL)
        wc, dc = weekly[3], daily[0]
        self.state.regime = engine.regime(wc, dc, self.profile)

    def _compute_tranche(self, symbol, card):
        """F8, computed here (not in engine.evaluate — that signature is locked
        by the M3 parity gate). Uses the LIVE tick price when available, else
        falls back to the last confirmed close — the champion card shows both,
        labeled, so the UI can tell which one sized the order. Called on every
        tick (cheap: cached pair info, pure arithmetic) so it never goes stale
        relative to the live entry price shown beside it."""
        if symbol not in self._pair_info_cache:
            self._pair_info_cache[symbol] = store.get_pair_info(self.conn, symbol)
        info = self._pair_info_cache[symbol]
        if info is None or info["ordermin"] is None or info["costmin"] is None:
            return
        ps = self.state.pair(symbol)
        live = ps.last_tick.last if ps.last_tick else None
        price = live if live else card.price
        if not price or price <= 0:
            return
        qty, mult = engine.tranche(card.score, card.required, info["ordermin"], info["costmin"],
                                    info["lot_decimals"], price)
        ps.tranche = TrancheInfo(qty=qty, mult=mult, price=price, price_is_live=bool(live))

    def _maybe_recompute_provisional(self, symbol):
        ps = self.state.pair(symbol)
        now_mono = time.monotonic()
        if now_mono - ps.last_provisional_ts < 1.0:
            return None  # F13 throttle: <=1/s per pair

        fw = store.get_forming(self.conn, symbol, 10080)
        fd = store.get_forming(self.conn, symbol, 1440)
        if fw is None or fd is None:
            return None  # cold start: haven't seen both forming bars yet — don't
            # consume the throttle window on a miss, or the update that WOULD
            # complete the pair (arriving moments later) gets throttled away.
        ps.last_provisional_ts = now_mono

        weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
        wo, wh, wl, wc, wvol = weekly
        weekly_p = (wo + [fw["o"]], wh + [fw["h"]], wl + [fw["l"]], wc + [fw["c"]], wvol + [fw["v"]])
        daily_p = (daily[0] + [fd["c"]],)
        ef = _elapsed_fraction(fw["ts"], 10080)
        card = engine.evaluate(symbol, weekly_p, daily_p, self.profile, provisional=True, elapsed_fraction=ef)
        ps.provisional = card
        if PROVISIONAL_ALERTS and card.status == "BUY":
            self._maybe_alert(symbol, card, kind="provisional")
        return card

    def _maybe_alert(self, symbol, card, kind):
        now = time.time()
        last_ts = store.last_alert_ts(self.conn, symbol, kind)
        if not engine.should_alert(last_ts, now, REALERT_HOURS):
            log.info("cooldown suppresses %s alert for %s (last=%.0f, now=%.0f, gap=%.0fs < %ds)",
                     kind, symbol, last_ts, now, now - last_ts, REALERT_HOURS * 3600)
            return
        alerter.fire(self.conn, symbol, card.price, card.score, card.denom, card.fired, kind=kind)
