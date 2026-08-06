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
from .config import (REALERT_HOURS, PROVISIONAL_ALERTS, STALE_SECS,
                     CLOSE_GRACE_SECS, CLOSE_POLL_SECS)

log = logging.getLogger("deepfield.ingest")

BTC_SYMBOL = "BTC/USD"

# §5(b) REST-confirm deferral ceiling: how long a clock-detected close may wait
# for a successful REST confirm before we flip+fire on the partial WS data
# anyway. The watchdog retries every CLOSE_POLL_SECS (~15s), so a transient
# outage just delays the close a few polls; a PERMANENT outage must not silence
# closes forever (operator no-blockers doctrine) — after this many seconds past
# the first failed confirm we proceed loudly on clock authority.
REST_CONFIRM_DEFER_SECS = 1800


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
        self._bg_tasks = set()   # strong refs so fire-and-forget dispatch isn't GC'd
        self._armed_buys = set()  # symbols already-BUY at startup that have fired their
                                  # one-shot boot arm this session (see handle_tick)
        self._last_fired = {}     # symbol -> last close INSTANT (interval_begin+interval*60)
                                  # that fired a confirmed alert/order. Keyed on the close
                                  # instant, NOT (symbol, interval): the WS feed AND the
                                  # clock-close watchdog emit CandleClosed for the same bar,
                                  # AND the daily+weekly bars close at the SAME instant every
                                  # Thu 00:00 UTC — both must collapse to ONE fire. (flip_closed's
                                  # rowcount can't be trusted: upsert_candle already set closed=1
                                  # on the clock-close/late-update path.) Fire once per NEW instant.
        self._rest_defer = {}     # (symbol, interval, forming_ts) -> unix ts of the FIRST
                                  # failed REST confirm for that bar. A clock-close whose
                                  # REST confirm fails is DEFERRED (no flip, no fire) so the
                                  # watchdog's next find_overdue pass retries it — until
                                  # REST_CONFIRM_DEFER_SECS, when we flip+fire loudly anyway.
                                  # Pruned on every successful confirm/close so it can't grow.
        self._boot_buys = None    # set of symbols BUY at BOOT (first startup_sweep). The
                                  # one-shot arm may fire ONLY these — otherwise a reconciler
                                  # resweep / 'f'-key that flips a symbol to BUY mid-session
                                  # would fire a live order from a path documented alert-silent
                                  # (Finding 3). None until the boot sweep populates it.
        if config.EXEC_MODE != "off":
            from . import executor as executor_mod
            self.executor = executor_mod.Executor(conn)
            log.warning("EXECUTION ENABLED — mode=%s (live leveraged orders on confirmed BUYs)",
                        config.EXEC_MODE)

    def _journal(self, kind, symbol, text):
        """Isolated journal emit (v6 JOURNAL view) — display-truth narration that
        must NEVER delay or drop the alert/order path. Never raises out."""
        try:
            store.journal(self.conn, kind, symbol, text)
        except Exception:
            log.exception("journal emit failed (%s %s) — path unaffected", kind, symbol)

    # ── event handlers ──────────────────────────────────────────────────────

    def handle_tick(self, ev: events.Tick):
        ps = self.state.pair(ev.symbol)
        ps.last_tick = ev
        ps.last_tick_ts = ev.ts
        # Keep the champion card's tranche priced off the live tick, not stale
        # from the last close/startup-sweep — found via the M6 export proof,
        # where "Live entry" and "Tranche" showed two different prices.
        if ps.confirmed is not None:
            self._compute_tranche(ev.symbol, ps.confirmed)
            self._maybe_arm_startup_buy(ev.symbol, ps)

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
        # Fast intervals (15m/1h) are INGESTED for display/analysis but must never
        # reach the scoring recompute or the order path — they are stored above and
        # we stop here. The edge-gate below keys on the close INSTANT per symbol, so
        # letting a 15m close through would both re-fire the confirmed-BUY path every
        # 15 minutes AND, via the boot seed, push _last_fired past the daily close
        # instant and suppress daily entries outright. See config.SIGNAL_INTERVALS.
        if ev.interval not in config.SIGNAL_INTERVALS:
            log.debug("candle closed %s/%d ts=%d — stored, not a signal interval",
                      ev.symbol, ev.interval, ev.interval_begin)
            self._rest_defer.pop((ev.symbol, ev.interval, ev.interval_begin), None)
            return
        # The old provisional card still counts the just-closed bar as forming —
        # honest display is "unknown" until fresh forming data arrives (seconds).
        self.state.pair(ev.symbol).provisional = None
        # Edge-gate the alert/exec on FIRST sight of this bar close. Two distinct
        # duplicate-sources exist: (1) the WS feed AND the clock-close watchdog both emit
        # CandleClosed for the same bar, and (2) the daily (1440) and weekly (10080) bars
        # close at the SAME instant every Thu 00:00 UTC (Kraken's weekly boundary) — one
        # confirmed-BUY verdict that, keyed per-interval, fired TWICE and double-placed a
        # live order (the ADA 2026-07-09 rung). Key the edge on the close INSTANT
        # (interval_begin + interval*60) per symbol so both collapse to ONE fire. (Not
        # flip_closed's rowcount: upsert_candle already flips closed=1 on the clock-close/
        # late-update path, so it would be 0 and wrongly SUPPRESS the fire on quiet pairs.)
        close_at = ev.interval_begin + ev.interval * 60
        fire = close_at > self._last_fired.get(ev.symbol, -1)
        if fire:
            self._last_fired[ev.symbol] = close_at
            # Coincident-border stale-weekly fix (audit 2026-07-13): weekly bars are
            # epoch-anchored, so EVERY weekly border is also a daily border (Thu 00:00
            # UTC). The dedup above gives exactly one of the two coincident events
            # fire=True — but flip_closed (top of this handler) flipped only THIS
            # event's bar, so the fire=True recompute would read a closed-series that
            # still EXCLUDES the other interval's just-completed bar (closed=0): six of
            # seven signals are weekly-driven, and daily-first ordering made the weekly
            # bar a whole week stale at evaluation time. Before the single recompute,
            # flip the OTHER interval's just-completed bar too — symmetric, so whichever
            # event arrives first flips both. flip_closed is idempotent (0 if already
            # closed); a missing row is the reconciler's gap to heal, not ours to block on.
            # This runs only inside the fire=True edge, so the ed74968 double-fire dedup
            # is untouched: the second coincident event still arrives with fire=False.
            for other in config.SIGNAL_INTERVALS:
                if other == ev.interval or close_at % (other * 60) != 0:
                    continue
                other_ts = close_at - other * 60
                n_other = store.flip_closed(self.conn, ev.symbol, other, other_ts)
                if n_other:
                    self.conn.commit()
                    log.info("coincident border: flipped %s/%d ts=%d closed alongside %d "
                             "close so the recompute sees BOTH completed bars",
                             ev.symbol, other, other_ts, ev.interval)
                else:
                    log.debug("coincident border: %s/%d ts=%d already closed or not in DB "
                              "yet (gap-heal covers it)", ev.symbol, other, other_ts)
                # Either way the bar is being handled here — drop any REST-confirm
                # deferral tracked for it.
                self._rest_defer.pop((ev.symbol, other, other_ts), None)
        # This bar is closed by whatever path got us here — prune its deferral entry.
        self._rest_defer.pop((ev.symbol, ev.interval, ev.interval_begin), None)
        self._recompute_confirmed(ev.symbol, fire=fire)
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
        buys = set()
        for p in config.PAIRS:
            symbol = p["ws"]
            weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
            card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
            self.state.pair(symbol).confirmed = card
            self._compute_tranche(symbol, card)
            if card.status == "BUY":
                buys.add(symbol)
        # The FIRST sweep (boot) defines which symbols the one-shot arm may fire. Later
        # sweeps (hourly reconciler / 'f'-key) republish state but must NOT expand it —
        # a symbol they flip to BUY only fires through a real close, never the boot arm.
        if self._boot_buys is None:
            self._boot_buys = buys
            # Seed the per-close fire-dedup from the DB so a restart doesn't re-fire a
            # bar that already closed (and fired) pre-restart: a late WS close for the
            # straddled border would otherwise fire AGAIN on top of the boot arm — two
            # orders from one restart. A genuinely newer bar (close instant > seed) still
            # fires. Seed the same close-INSTANT the edge compares (interval_begin +
            # interval*60), per symbol; the latest daily close instant dominates the weekly.
            # SIGNAL_INTERVALS only: this takes the MAX close instant per symbol, so a
            # fast interval closing minutes ago would seed the dedup PAST the daily
            # close instant and suppress the next daily fire entirely — entries would
            # silently stop after a restart. Only bars that can fire may seed it.
            for p in config.PAIRS:
                for interval in config.SIGNAL_INTERVALS:
                    mt = store.max_closed_ts(self.conn, p["ws"], interval)
                    if mt is not None:
                        self._last_fired[p["ws"]] = max(
                            self._last_fired.get(p["ws"], -1), mt + interval * 60)
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
            # SIGNAL_INTERVALS only. This watchdog exists so a silent border cannot
            # delay a SCORING close, and each overdue bar costs a REST confirm — at
            # 15m granularity something is always overdue, which would turn a safety
            # net into a constant REST drip. Fast intervals ride the hourly gap-heal.
            for interval in config.SIGNAL_INTERVALS:
                f = store.get_forming(self.conn, ws, interval)
                if f is None:
                    continue
                if now >= f["ts"] + interval * 60 + grace:
                    overdue.append((ws, rest_by_ws[ws], interval, f["ts"]))
        return overdue

    def apply_rest_confirm(self, ws, interval, forming_ts, rows):
        """REST-confirm a clock-detected close: upsert the authoritative closed
        bar (and the new forming bar if present), then run the exact same
        close path a WS CandleClosed would.

        Confirm-or-defer (audit 2026-07-13): if the REST fetch FAILED (rows is
        None/empty — outage), do NOT flip the bar closed and do NOT fire — that
        would promote partial pre-outage WS data to closed truth and place live
        orders on it. Leave the bar forming; the watchdog's next find_overdue
        pass (~every CLOSE_POLL_SECS) retries. After REST_CONFIRM_DEFER_SECS of
        failed confirms for the same bar, flip+fire anyway with a loud warning
        so a permanent REST outage can't silence closes forever (no-blockers)."""
        now = int(time.time())
        key = (ws, interval, forming_ts)
        found = False
        if rows:
            self._rest_defer.pop(key, None)   # confirm succeeded — clear any deferral
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
        else:
            # REST confirm failed (outage). Defer: no flip, no fire — retry next pass.
            first_seen = self._rest_defer.setdefault(key, now)
            waited = now - first_seen
            if waited < REST_CONFIRM_DEFER_SECS:
                log.warning("clock-close: REST confirm FAILED for %s/%s ts=%d — deferring "
                            "flip/fire (%ds of %ds); watchdog will retry",
                            ws, interval, forming_ts, waited, REST_CONFIRM_DEFER_SECS)
                return
            # Deferral ceiling hit: proceed on clock authority + partial WS data,
            # loudly. Hourly reconciler will true-up the values afterwards.
            self._rest_defer.pop(key, None)
            log.warning("clock-close: REST confirm still failing after %ds for %s/%s ts=%d — "
                        "DEFERRAL CEILING hit; flipping + firing on partial WS data "
                        "(no-blockers: a permanent REST outage must not silence closes)",
                        waited, ws, interval, forming_ts)
        if not found and rows:
            # REST answered but didn't return the bar (shouldn't happen) — flip on
            # clock authority alone; hourly reconciler will true-up the values.
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
                # Prune deferral entries for bars no longer overdue (e.g. closed by
                # gap-heal, which upserts closed=1 without a CandleClosed event) so
                # self._rest_defer cannot grow without bound.
                live_keys = {(ws, interval, forming_ts)
                             for ws, _rest, interval, forming_ts in overdue}
                for k in list(self._rest_defer):
                    if k not in live_keys:
                        self._rest_defer.pop(k, None)
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

    def _recompute_confirmed(self, symbol, fire=True):
        """Recompute the confirmed card (always, for display). fire=False recomputes
        WITHOUT alerting/ordering — used for a duplicate close so the same bar can't
        place two live orders (Finding 2). A genuine close passes fire=True."""
        weekly, daily = store.load_weekly_daily_closed(self.conn, symbol)
        card = engine.evaluate(symbol, weekly, daily, self.profile, provisional=False)
        self.state.pair(symbol).confirmed = card
        self._compute_tranche(symbol, card)
        if fire and card.status == "BUY":
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
        # Persist the regime label so the executor (which holds only the DB conn, not
        # AppState) can gate accumulation on it (config.ACCUMULATE_ONLY_IN_BEAR). Guarded
        # — a meta write must never break the regime recompute / writer.
        try:
            store.meta_set(self.conn, "regime", getattr(self.state.regime, "label", "UNKNOWN"))
        except Exception:
            log.exception("regime meta persist failed (regime state unaffected)")

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

    def _maybe_arm_startup_buy(self, symbol, ps):
        """Fire a symbol that is ALREADY a confirmed BUY when this process starts.

        The alert/exec path is edge-triggered on a live candle *close*
        (_recompute_confirmed). startup_sweep() publishes the confirmed cards but
        deliberately does NOT fire — so a symbol sitting at BUY when the bot
        (re)starts would never place an order until its next daily/weekly close
        (up to 7 days). This one-shot arm closes that gap: on the first fresh tick
        for such a symbol, run the same _maybe_alert path a close would. Fired at
        most once per symbol per process (self._armed_buys); real closes re-fire
        independently. F10 cooldown still applies (with REALERT_HOURS=0 it fires;
        set >0 to let a recent alert suppress the boot re-fire). NOTE: because it
        re-arms every launch, each restart re-fires every open BUY — intended
        under the operator's cooldown-off/no-blockers stance."""
        card = ps.confirmed
        if (symbol in self._armed_buys or card is None or card.status != "BUY"
                or engine.is_stale(ps.tick_age(), STALE_SECS)
                # Only symbols that were BUY at BOOT may boot-arm. A symbol that became
                # BUY later (reconciler resweep / 'f'-key) is NOT here, so the quiet
                # resweep stays quiet; its real close fires it instead (Finding 3).
                or self._boot_buys is None or symbol not in self._boot_buys):
            return
        # If a pending entry bid already rests for this symbol, its thesis is already
        # expressed on the book — the arm's whole purpose (bridge the up-to-7-day wait
        # for the next close) is already served. Skip the re-fire so a restart doesn't
        # stack a redundant bid on the resting one; the duplicates were restart-driven,
        # not new pyramid steps. Consume the one-shot; the next real close fires normally.
        # MODE-SCOPED: this gates whether a real entry gets placed, so it must ask
        # about THIS book. A paper run is seeded from a live DB snapshot, so both
        # ledgers share the file — unscoped, a leftover row from the other book
        # silently suppresses a legitimate entry, and the log line explains the skip
        # by pointing at a bid that does not exist in the book being traded.
        _bmode = self.executor.mode if self.executor is not None else config.EXEC_MODE
        if store.has_pending_entry(self.conn, symbol,
                                   mode=_bmode if _bmode in ("live", "paper") else None):
            self._armed_buys.add(symbol)
            log.info("startup-arm: %s already has a resting pending entry — skipping re-fire", symbol)
            self._journal("order", symbol, "boot-arm skipped — entry already resting")
            return
        # Own each level once: if we already hold an OPEN rung within a ladder step of the
        # live price, the boot arm would just re-stack a near-market rung on EVERY restart
        # (it bids near the live tick, not at a dip) — a slow restart-driven drip. The
        # has_pending_entry check above covers a resting BID; this covers an already-FILLED
        # rung near price. Reuses the same guard continuous laddering uses. Only when an
        # executor exists (off mode is signal-only — no rungs to stack).
        live_px = ps.last_tick.last if ps.last_tick else None
        if (live_px and self.executor is not None
                and self.executor._owns_level_near(symbol, live_px, config.LADDER_STEP_PCT)):
            self._armed_buys.add(symbol)
            log.info("startup-arm: %s already owns a rung within a step of %.6g — skipping "
                     "re-fire (own each level once)", symbol, live_px)
            self._journal("order", symbol, "boot-arm skipped — own a rung near price")
            return
        self._armed_buys.add(symbol)   # one-shot: never retry this symbol on later ticks
        log.info("startup-arm: %s already confirmed BUY %d/%d — firing on first fresh tick",
                 symbol, card.score, card.denom)
        self._maybe_alert(symbol, card, kind="confirmed")

    def _maybe_alert(self, symbol, card, kind):
        now = time.time()
        last_ts = store.last_alert_ts(self.conn, symbol, kind)
        if not engine.should_alert(last_ts, now, REALERT_HOURS):
            log.info("cooldown suppresses %s alert for %s (last=%.0f, now=%.0f, gap=%.0fs < %ds)",
                     kind, symbol, last_ts, now, now - last_ts, REALERT_HOURS * 3600)
            return
        if kind == "confirmed":
            self._journal("detect", symbol, f"{card.score}/{card.denom} confirmed BUY")
        # SPEC §11: the alert message carries the LIVE price. The card's price
        # is the last closed daily close — hours stale at alert time.
        ps = self.state.pair(symbol)
        price = card.price
        if ps.last_tick is not None and not engine.is_stale(ps.tick_age(), STALE_SECS):
            price = ps.last_tick.last
        # The alert chain (sound/notify/Telegram) and live order placement do
        # BLOCKING network I/O with retries — on the writer that stalls the event
        # loop and can trip the WS watchdog. Offload to a thread (own DB conn,
        # sqlite isn't cross-thread) when a loop is running; run inline otherwise
        # (--once / tests, where self.conn is the right connection).
        do_exec = (kind == "confirmed" and self.executor is not None)
        args = (symbol, price, card.score, card.denom, list(card.fired), kind, do_exec, card)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch(self.conn, *args)     # sync path (no event loop)
            return
        # Hold a strong reference: asyncio only weakly tracks tasks, so an
        # unreferenced ensure_future can be GC'd before it runs — dropping the
        # alert/order. Keep it in a set until it completes.
        task = asyncio.ensure_future(asyncio.to_thread(self._dispatch_threaded, *args))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _dispatch_threaded(self, *args):
        conn = store.connect(config.DB_PATH)     # thread-local connection
        try:
            self._dispatch(conn, *args)
        except Exception:
            log.exception("offloaded alert/exec dispatch failed")
        finally:
            conn.close()

    def _dispatch(self, conn, symbol, price, score, denom, fired, kind, do_exec, card):
        # Place the live order FIRST and in isolation. The alert chain is DECORATION and
        # must never delay or, on a raise, silently DROP a live order — `alerter.fire`
        # runs unguarded DB/format code (e.g. a 'database is locked' on the alerts
        # insert, a Telegram timeout) and previously sat upstream of place_entry inside
        # the same try, so any such raise skipped the order. Order first; alert wrapped
        # so its failure is logged, never propagated (decoration must not kill the trade).
        if do_exec:
            from . import executor as executor_mod
            ex = executor_mod.Executor(conn)
            ex.mode = config.EXEC_MODE
            ex.place_entry(symbol, price, card)
        try:
            alerter.fire(conn, symbol, price, score, denom, fired, kind=kind)
        except Exception:
            log.exception("alerter.fire failed for %s (kind=%s) — order already handled; alert dropped",
                          symbol, kind)
