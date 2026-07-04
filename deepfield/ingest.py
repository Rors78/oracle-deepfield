"""Single writer: consumes the event queue, persists to DB, updates published
state, triggers confirmed/provisional recompute, and gates the alert chain via
F10 cooldown. SPEC §5, invariants 1-3, F5/F10/F13.

The stream is transport, not truth (invariant 3): both confirmed and
provisional recompute re-read the closed/forming series from the DB rather
than accumulating a series in memory from events.

Also owns the §5(b) clock-close fallback: the WS ohlc feed sends NOTHING across
an interval border until the next trade, so a low-volume close can pass silently.
The clock watchdog finds forming bars past deadline (+grace), REST-confirms the
closed bar, flips it, and runs the same confirmed-recompute path a WS close would.
"""
import time
import asyncio
import logging

from . import store
from . import engine
from . import events
from . import alerter
from . import config
from .profiles import FULL
from .state import TrancheInfo
from .config import (REALERT_HOURS, PROVISIONAL_ALERTS, STALE_SECS, FLASH_SECS,
                     CLOSE_GRACE_SECS, CLOSE_POLL_SECS)

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
        # Execution is OFF unless EXEC_MODE flips (docs/RULINGS override). When
        # off, self.executor is None and the confirmed-BUY path is signal-only.
        self.executor = None
        if config.EXEC_MODE != "off":
            from . import executor as executor_mod
            self.executor = executor_mod.Executor(conn)
            log.warning("EXECUTION ENABLED — mode=%s (live leveraged orders on confirmed BUYs)",
                        config.EXEC_MODE)

    # ── event handlers ──────────────────────────────────────────────────────

    def handle_tick(self, ev: events.Tick):
        ps = self.state.pair(ev.symbol)
        if ps.last_tick is not None and ev.last != ps.last_tick.last:
            ps.flash_color = "green" if ev.last > ps.last_tick.last else "red"
            ps.flash_until = time.monotonic() + FLASH_SECS
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
        # Monotonic guard: a late-arriving update for the OLD bar (in flight
        # during a border) must never wind the countdown backwards.
        self._advance_countdown(ev.interval, ev.interval_begin)
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
        # The old provisional card still counts the just-closed bar as forming —
        # honest display is "unknown" until fresh forming data arrives (seconds).
        self.state.pair(ev.symbol).provisional = None
        self._recompute_confirmed(ev.symbol)
        if ev.symbol == BTC_SYMBOL:
            self._recompute_regime()

    def handle_link_up(self, ev: events.LinkUp):
        entry = self.state.links.setdefault(ev.name or "?", {"up": False, "reconnects": 0})
        entry["up"] = True
        entry["reconnects"] = ev.reconnect_count
        self.state.link_up = self.state.link_status() == "UP"
        self.state.reconnect_count = self.state.total_reconnects()

    def handle_link_down(self, ev: events.LinkDown):
        entry = self.state.links.setdefault(ev.name or "?", {"up": False, "reconnects": 0})
        entry["up"] = False
        self.state.link_up = self.state.link_status() == "UP"

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
            if handler is None:
                continue
            try:
                handler(ev)
            except Exception:
                # Invariant 1/2: the single writer must never die. A poisoned
                # event is logged loudly and skipped; the stream continues.
                log.exception("writer: handler failed for %s — event skipped: %r",
                              type(ev).__name__, ev)

    # ── startup ──────────────────────────────────────────────────────────────

    def startup_sweep(self):
        """Populate confirmed ScoreCards + regime from the DB's closed series
        right after warm-backfill — otherwise the TUI launches blank and stays
        blank until the next real candle close (up to 7 days for weekly).
        Also used by `--once` and after reconciler repairs. No alerts fire here:
        this is not a live transition, just publishing already-true state (F10
        cooldown is keyed off real confirmed-BUY *events*)."""
        for p in config.PAIRS:
            symbol = p["ws"]
            weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
            card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
            self.state.pair(symbol).confirmed = card
            self._compute_tranche(symbol, card)
        self._recompute_regime()

    # ── §5(b) clock-close fallback ───────────────────────────────────────────

    def find_overdue(self, now=None, grace=CLOSE_GRACE_SECS):
        """Forming bars past their close deadline (+grace) — the silent-border
        case where no trade has arrived to roll the WS feed. Returns
        [(ws_symbol, rest_pair, interval, forming_ts), ...]."""
        now = time.time() if now is None else now
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}
        overdue = []
        for p in config.PAIRS:
            ws = p["ws"]
            for interval in config.INTERVALS:
                f = store.get_forming(self.conn, ws, interval)
                if f is None:
                    continue
                if now >= f["ts"] + interval * 60 + grace:
                    overdue.append((ws, rest_by_ws[ws], interval, f["ts"]))
        return overdue

    def apply_rest_confirm(self, ws, interval, forming_ts, rows):
        """REST-confirm a clock-detected close: upsert the authoritative closed
        bar (and the new forming bar if present), then run the exact same
        close path a WS CandleClosed would."""
        now = int(time.time())
        found = False
        if rows:
            for r in rows[-4:]:  # only the recent tail is relevant
                ts = int(r[0])
                if ts < forming_ts:
                    continue
                closed = 1 if now >= ts + interval * 60 else 0
                store.upsert_candle(self.conn, ws, interval, ts,
                                     float(r[1]), float(r[2]), float(r[3]),
                                     float(r[4]), float(r[6]), closed)
                if ts == forming_ts:
                    found = True
                if closed == 0:
                    self._advance_countdown(interval, ts)
            self.conn.commit()
        if not found:
            # REST didn't return the bar (shouldn't happen) — flip on clock
            # authority alone; hourly reconciler will true-up the values.
            log.warning("clock-close: REST confirm missing bar %s/%s ts=%d — flipping on clock",
                        ws, interval, forming_ts)
        log.info("CLOCK CLOSE confirmed %s/%s ts=%d (silent border — no trade rolled the feed)",
                 ws, interval, forming_ts)
        self.handle_candle_closed(events.CandleClosed(ws, interval, forming_ts))

    async def clock_close_watchdog(self, fetch_ohlc, poll_secs=CLOSE_POLL_SECS):
        """Poll for overdue forming bars; REST-confirm each (throttled fetch in
        a thread, DB writes back on the loop — single-writer preserved)."""
        while True:
            await asyncio.sleep(poll_secs)
            try:
                overdue = self.find_overdue()
                for ws, rest, interval, forming_ts in overdue:
                    rows = await asyncio.to_thread(fetch_ohlc, rest, interval)
                    self.apply_rest_confirm(ws, interval, forming_ts, rows)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("clock-close watchdog pass failed (will retry)")

    # ── recompute + alert gating ────────────────────────────────────────────

    def _advance_countdown(self, interval, interval_begin):
        if interval == 1440:
            if interval_begin > (self.state.daily_interval_begin or 0):
                self.state.daily_interval_begin = interval_begin
        elif interval == 10080:
            if interval_begin > (self.state.weekly_interval_begin or 0):
                self.state.weekly_interval_begin = interval_begin

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
        labeled. Called on every tick (cached pair info, pure arithmetic).
        Guarded: a sizing edge case must never kill the writer."""
        try:
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
            qty, mult = engine.tranche(card.score, card.required, info["ordermin"],
                                        info["costmin"], info["lot_decimals"], price)
            ps.tranche = TrancheInfo(qty=qty, mult=mult, price=price, price_is_live=bool(live))
        except Exception:
            log.exception("tranche computation failed for %s (display-only, skipped)", symbol)

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
        # SPEC §11: the alert message carries the LIVE price. The card's price
        # is the last closed daily close — hours stale at alert time.
        ps = self.state.pair(symbol)
        price = card.price
        if ps.last_tick is not None and not engine.is_stale(ps.tick_age(), STALE_SECS):
            price = ps.last_tick.last
        alerter.fire(self.conn, symbol, price, card.score, card.denom, card.fired, kind=kind)
        # Execution rides the SAME confirmed-BUY-past-cooldown gate as the alert:
        # at most one entry per symbol per REALERT_HOURS. Never provisional.
        if kind == "confirmed" and self.executor is not None:
            self.executor.place_entry(symbol, price, card)
