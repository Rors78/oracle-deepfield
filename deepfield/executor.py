"""Live Kraken spot-margin executor — deterministic. Operator override (RULINGS).

Confirmed BUY -> risk-rail gate -> size (2% equity risked, off the stop) -> open
a leveraged long on the :BTNL margin book -> rest a protective stop -> record.
No learning brain, no calibration period. EXEC_MODE gates everything:

  off      : no-op (default) — the ingest hook does nothing
  paper    : simulate the fill + stop, record to `orders`, zero network
  validate : send the REAL order with validate=true — Kraken checks pair name,
             leverage, precision, minimums and returns the parsed order WITHOUT
             executing. The proof gate before a cent is real.
  live     : real AddOrder + real resting stop

Mechanics (pair name, leverage, post-only, :BTNL trigger=index, price precision)
are hydra's field-verified patterns; nothing here is guessed.

AUDITOR NOTE: the auto-rails (drawdown kill-switch, loss caps, max-positions),
the alert cooldown, and re-fire-on-restart are switched OFF *by design* — see
docs/AUDIT_ORIENTATION.md before flagging removed safeguards. Containment rests
on min-sizing (EXEC_SIZE_MODE), NOT the rails; protective stops ARE placed
(PROTECTIVE_STOP=True). Read that doc for the intended model and the parts that
genuinely warrant scrutiny (sizing/rounding, the pending->open->stopped machine).
"""
import os
import json
import math
import time
import types
import logging
import datetime

from . import store
from . import broker
from . import config
from . import engine
from . import rest_client

log = logging.getLogger("deepfield.exec")


def _round_down(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f) / f


def _round_price(x, decimals):
    return round(x, decimals)


# Kraken echoes a pair as a canonical key, an X-prefixed altname, or a base with a
# ':SUFFIX' (margin). Normalize all three to the canonical `rest` key so ledger rows,
# OpenPositions, and OpenOrders compare on ONE identity. (Hydra field-verified map.)
_ALT_TO_KEY = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD",
               "XRPUSD": "XXRPZUSD", "LTCUSD": "XLTCZUSD",
               "XLMUSD": "XXLMZUSD", "ZECUSD": "XZECZUSD"}


def _norm_pair_key(pr):
    base = str(pr or "").split(":")[0]
    if not base:
        return ""
    return base if base in {p["rest"] for p in config.PAIRS} else _ALT_TO_KEY.get(base, base)


def _age_secs(ts_iso):
    """Seconds since an ISO-8601 order timestamp; 0.0 (treated as never-stale) if the
    value is missing or unparseable, so a bad ts can never trigger a cancel."""
    try:
        t = datetime.datetime.fromisoformat(ts_iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _entry_ttl_expired(ts_iso):
    return config.ENTRY_TTL_SECS > 0 and _age_secs(ts_iso) > config.ENTRY_TTL_SECS


def _new_userref():
    """Fresh Kraken client order id (positive int32). Random, not sequential — two
    processes/threads must never mint the same ref, and Kraken only needs it unique
    enough to search by (collisions across our own history are harmless: recovery
    matches OpenOrders/ClosedOrders, newest first, and refs live for minutes)."""
    import random
    return random.randint(1, 2**31 - 1)


# symbol -> public REST pair id, for the live-price clamp on ladder rungs.
_REST_PAIR = {p["ws"]: p["rest"] for p in config.PAIRS}

# Re-ladder backoff (module-level: an Executor is constructed per poll cycle, so
# per-instance state would reset every ~15s). A symbol whose re-place attempt did
# not produce a resting bid (ladder floor, own-level, regime gate, reject) is not
# retried for this long — keeps a chain holding at its floor from spamming.
_RELADDER_RETRY_SECS = 600
_reladder_next = {}     # symbol -> time.monotonic() of next allowed attempt
_seed_next = {}         # symbol -> time.monotonic() of next allowed seed attempt
# Runtime exchange-truth sweep cadence (audit 2026-07-13 #1): module-level because
# an Executor is constructed per poll cycle. 0.0 = run on the first live cycle so a
# stop that fired during a bot outage is caught minutes after boot, not next restart.
_recon_next = 0.0
# Stack-floor pause narration (audit Wave 1): module-level (Executor is rebuilt
# per cycle) so the "seeds/rungs paused" line is EDGE-triggered — journaled once
# on entry and once on exit (with duration) — instead of the per-attempt spam the
# audit found (531 identical safety events in 24h). None = not paused; a monotonic
# ts = paused since. Operator ALERTING for the danger now lives in the price-space
# defense engine (app._run_defense); this stays a quiet book-state journal note.
_stack_pause_since = None
# T/P trough-ratchet narration: module-level for the same rebuilt-per-cycle reason.
# Tracks the last JOURNALED trough so the ratchet note is edge-triggered on ≥1%
# steps down, not written on every poll of a sliding decline.
_tp_trough_noted = None
# Ambiguous-AddOrder recovery: rows born from a network-unknown AddOrder carry a
# userref and txid NULL; give Kraken this long to show the order before concluding
# it never landed (poll cadence is ~15s, so this is ~20 attempts).
_USERREF_RESOLVE_SECS = 300


class Executor:
    def __init__(self, conn):
        self.conn = conn
        self.mode = config.EXEC_MODE
        # sym -> (surplus_volume, first_seen_ts): how long a stable untracked surplus
        # has gone unclaimed, so _adopt_surplus can tell a standing external position
        # from a fill that has not been booked yet. In-memory by design — a restart
        # restarts the clock rather than adopting on stale evidence.
        self._surplus_seen = {}

    def _journal(self, kind, symbol, text):
        """Isolated journal emit (v6 JOURNAL view). DISPLAY-ONLY narration — a
        failure here must NEVER delay or drop a fill/stop/order, so every emit
        goes through this try/except (same rule as the alerter dispatch fix).
        Never raises into the money path."""
        try:
            store.journal(self.conn, kind, symbol, text)
        except Exception:
            log.exception("journal emit failed (%s %s) — trade path unaffected", kind, symbol)

    def _safety(self, kind, symbol, text):
        """Safety event -> journal + operator alert (sound/notify/telegram, throttled
        in the alerter). Isolated — never raises into the money path. Lazy import
        keeps executor importable in test contexts without audio deps."""
        self._journal("safety", symbol or "", f"[{kind}] {text}")
        try:
            from . import alerter
            alerter.fire_safety(kind, symbol, text)
        except Exception:
            log.exception("safety alert failed (%s %s) — trade path unaffected", kind, symbol)

    # ── portfolio + rails ────────────────────────────────────────────────────

    def portfolio_value(self):
        if self.mode == "live":
            eq = broker.trade_balance()
            if eq is not None:
                self._update_peak(eq)
                return eq
            log.error("could not read live equity (TradeBalance) — cannot size")
            return None
        return config.PAPER_PORTFOLIO_USD

    def _update_peak(self, equity):
        try:
            peak = float(store.meta_get(self.conn, "peak_equity", 0) or 0)
        except (TypeError, ValueError):
            peak = 0.0
        if equity > peak:
            store.meta_set(self.conn, "peak_equity", equity)

    def rails_ok(self, equity):
        """Deterministic hard limits. (ok: bool, reason: str). The manual HALT file
        is always honored (operator's hand-on-switch); the AUTOMATIC circuit
        breakers below are gated by RAILS_ENABLED (operator override: default off)."""
        if os.path.exists(config.HALT_FILE):
            return False, f"HALT file present ({config.HALT_FILE})"
        if not config.RAILS_ENABLED:
            return True, "ok (auto-rails disabled)"
        # Fail-safe: in live mode an unknown equity means the kill-switch cannot be
        # evaluated — do NOT trade blind (min-size sizing ignores equity, so without
        # this the drawdown halt would be silently bypassed on a TradeBalance failure).
        if self.mode == "live" and equity is None:
            return False, "equity unavailable — cannot verify kill-switch (blocking)"
        # Cap counts committed exposure: filled positions AND resting entry limits
        # (a 'pending' limit will become a position — counting only 'open' lets many
        # rest under the cap and fill together, breaching MAX_OPEN_POSITIONS).
        n = store.committed_position_count(self.conn, mode=self.mode)
        if n >= config.MAX_OPEN_POSITIONS:
            return False, f"max open positions ({n}/{config.MAX_OPEN_POSITIONS})"
        try:
            peak = float(store.meta_get(self.conn, "peak_equity", 0) or 0)
        except (TypeError, ValueError):
            peak = 0.0
        if peak > 0 and equity is not None and equity < peak * (1 - config.KILL_SWITCH_DD_PCT):
            return False, (f"KILL SWITCH: equity ${equity:.2f} < {(1-config.KILL_SWITCH_DD_PCT)*100:.0f}% "
                           f"of peak ${peak:.2f} — manual reset (clear peak_equity)")
        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        wk0 = (now - datetime.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        dpl = store.realized_pnl_since(self.conn, day0)
        wpl = store.realized_pnl_since(self.conn, wk0)
        if dpl <= -config.DAILY_LOSS_LIMIT_USD:
            return False, f"daily loss limit (${dpl:.2f} <= -${config.DAILY_LOSS_LIMIT_USD})"
        if wpl <= -config.WEEKLY_LOSS_LIMIT_USD:
            return False, f"weekly loss limit (${wpl:.2f} <= -${config.WEEKLY_LOSS_LIMIT_USD})"
        return True, "ok"

    # ── sizing ───────────────────────────────────────────────────────────────

    def compute_stop(self, symbol, entry, card):
        """Stop price. STOP_MODE=support uses the 52w-low/W-support from the
        scorecard (bottom-thesis invalidation); clamped to [MIN,MAX]% of entry
        so it's never absurdly tight or wide."""
        support = getattr(card, "low_52w", None) if card is not None else None
        if config.STOP_MODE == "support" and support and 0 < support < entry:
            stop = support
        else:
            stop = entry * (1 - config.STOP_PCT)
        min_stop = entry * (1 - config.STOP_MAX_PCT)   # widest allowed (lowest price)
        max_stop = entry * (1 - config.STOP_MIN_PCT)   # tightest allowed (highest price)
        return max(min_stop, min(stop, max_stop))

    def _min_volume(self, ordermin, costmin, entry, lot_dec):
        """Smallest placeable order: >= ordermin AND cost >= costmin, on the lot
        grid (rounded UP so it never lands under either floor)."""
        need = max(ordermin, (costmin / entry) if entry > 0 else 0.0)
        if lot_dec is None:
            return need
        f = 10 ** lot_dec
        return math.ceil(need * f) / f

    def _owns_level_near(self, symbol, price, pct):
        """True if an OPEN position for symbol already sits within `pct` (fraction)
        of `price`. Used by continuous laddering to own each price level once
        instead of re-buying the same band as price churns."""
        if price <= 0:
            return False
        rows = self.conn.execute(
            "SELECT entry FROM orders WHERE symbol=? AND status='open' AND entry IS NOT NULL "
            "AND mode=?", (symbol, self.mode)).fetchall()
        return any(abs(e - price) <= pct * price for (e,) in rows if e)

    def size(self, symbol, entry, stop, leverage, equity, card=None):
        """Returns a sizing dict or None.
        EXEC_SIZE_MODE='min' (default): buy the minimum order — tiny, no liquidation
        worry. With a `card`, the min order is CONVICTION-WEIGHTED (Tier 1): the same
        engine.tranche the champion card displays scales the min-fill by score-over-
        required (delta 0 -> 1.0x STARTER, +1 -> 2.0x, +2 -> 3.0x; config.CONVICTION),
        so the live fill
        matches the shown tranche. No card -> flat 1.0x min (ladder/plan preview).
        'risk': volume = risk_usd/(entry-stop), margin-capped, min-floored."""
        info = store.get_pair_info(self.conn, symbol) or {}
        lot_dec = info.get("lot_decimals")
        ordermin = info.get("ordermin") or 0.0
        costmin = info.get("costmin") or 0.0
        stop_dist = entry - stop
        if entry <= 0:
            return None

        if config.EXEC_SIZE_MODE == "min":
            volume = self._min_volume(ordermin, costmin, entry, lot_dec)
            mult = 1.0
            if card is not None:
                # Conviction weighting: reuse the exact engine.tranche the display
                # uses so the fill == the shown qty. Guarded — conviction is a bonus
                # ON TOP of the min-fill floor, never a reason an order fails to size.
                try:
                    cvol, cmult = engine.tranche(card.score, card.required,
                                                 ordermin, costmin, lot_dec, entry)
                    if cvol > 0:
                        volume, mult = cvol, cmult
                except Exception:
                    log.warning("size %s: conviction tranche failed — flat min", symbol)
            # Operator size multiplier (2026-07-13): scale the (conviction-weighted)
            # min fill by SIZE_MULT, rounded DOWN to the lot grid but never below the
            # min-fill floor. Kept OUT of conviction_mult so the conviction-scaled
            # notional ceiling in the placement paths keeps its own meaning.
            smult = max(1.0, float(getattr(config, "SIZE_MULT", 1.0) or 1.0))
            if smult > 1.0 and volume > 0:
                scaled = volume * smult
                if lot_dec is not None:
                    scaled = _round_down(scaled, lot_dec)
                volume = max(volume, scaled)
            if volume <= 0:
                return None
            notional = volume * entry
            return {
                "volume": volume, "notional": notional, "margin": notional / leverage,
                "risk_usd": 0.0, "actual_risk": volume * max(0.0, stop_dist),
                "capped": False, "floored_to_min": mult == 1.0, "size_mode": "min",
                "conviction_mult": mult, "size_mult": smult,
            }

        # "risk" mode
        if stop_dist <= 0 or equity is None or equity <= 0:
            return None
        risk_usd = config.RISK_PCT * equity
        volume = risk_usd / stop_dist
        # margin cap: a single position posts at most MARGIN_CAP_PCT of equity
        max_margin = equity * config.MARGIN_CAP_PCT
        max_vol_by_margin = (max_margin * leverage) / entry
        capped = volume > max_vol_by_margin
        volume = min(volume, max_vol_by_margin)
        # Kraken floors
        floored_to_min = False
        min_vol = max(ordermin, (costmin / entry) if entry > 0 else 0.0)
        if volume < min_vol:
            volume = min_vol
            floored_to_min = True
        if lot_dec is not None:
            volume = _round_down(volume, lot_dec)
            if volume < min_vol:  # rounding pushed us under — bump one lot
                volume = _round_down(min_vol + (10 ** -lot_dec), lot_dec)
        if volume <= 0:
            return None
        notional = volume * entry
        margin = notional / leverage
        actual_risk = volume * stop_dist   # if floored/capped, real risk != target
        return {
            "volume": volume, "notional": notional, "margin": margin,
            "risk_usd": risk_usd, "actual_risk": actual_risk,
            "capped": capped, "floored_to_min": floored_to_min, "size_mode": "risk",
        }

    def plan(self, symbol, entry, card, equity):
        """Dry-run order plan for display — what live execution WOULD place, no
        order sent. Pure arithmetic + cached pair info. Returns dict or None."""
        if not entry or not equity or symbol not in config.MARGIN_PAIR:
            return None
        leverage = config.PER_PAIR_LEVERAGE.get(symbol)   # fixed, hardcoded
        if not leverage:
            return None
        stop = self.compute_stop(symbol, entry, card)
        s = self.size(symbol, entry, stop, leverage, equity, card=card)
        if not s:
            return None
        return {"leverage": leverage, "stop": stop, "entry": entry, **s}

    # ── placement ────────────────────────────────────────────────────────────

    def _accumulation_allowed(self):
        """FORK A regime gate (config.ACCUMULATE_ONLY_IN_BEAR): accumulate in weakness,
        pause in confirmed strength. Returns (ok, reason). FAILS OPEN — only an
        unambiguous BULL regime (config.NO_ACCUMULATE_REGIMES) pauses new entries/rungs;
        an unknown/missing/other regime still accumulates, so a stale or unavailable
        regime can never silently halt entries (operator no-blockers stance). The
        regime label is persisted to meta by ingest._recompute_regime."""
        if not getattr(config, "ACCUMULATE_ONLY_IN_BEAR", False):
            return True, ""
        regime = store.meta_get(self.conn, "regime", None)
        blocked = getattr(config, "NO_ACCUMULATE_REGIMES", ("BULL",))
        if regime and regime in blocked:
            return False, f"regime={regime} (accumulate only outside {tuple(blocked)})"
        return True, ""

    def _stack_margin_ok(self):
        """Margin-stack floor (audit 2026-07-13 #2): Kraken margin-calls at ml<=80%
        and force-liquidates from ml<=40% — bypassing every stop. Below
        config.MARGIN_LEVEL_STACK_FLOOR_PCT, SEEDS and LADDER RUNGS pause (they only
        GROW the book); confirmed-BUY signal entries are never gated here (operator
        no-blockers stance). Reads the margin level the app loop persisted to
        meta['web_live'] ≤15s ago — no extra API call. FAILS OPEN: a stale (>120s),
        missing, or unparseable level never pauses anything. Returns (ok, reason)."""
        try:
            # A T/P flatten in progress (limit closes resting/chasing) owns the book:
            # seeds/rungs placed now would be canceled by the very next flatten pass —
            # a placement/cancel war. Restack resumes the cycle after completion.
            if str(store.meta_get(self.conn, "tp_flatten_active", "") or "") == "1":
                return False, "T/P flatten in progress"
        except Exception:
            pass                                   # fail open, same as the floor read
        # L_eff ceiling gate (rails re-arm 2026-07-30): the stress telemetry
        # already computes the effective leverage that survives the worst 1-day
        # basket move on record (meta stress_state, app._poll_stress_threaded,
        # 300s cadence) — it used to only alert. Here it gates GROWTH: seeds and
        # rungs pause while lev_now > lev_ceiling. Composition-independent
        # backstop the ML floor can't give: an all-10x book passes the ML-200
        # floor up to ~5x effective — above the ~4x survivable ceiling. Same
        # fail-open idiom as the floor read below: missing/stale (>600s, two
        # missed stress polls)/flat/inf never pauses anything. Entry-gating
        # only — never closes positions (the reverse gear is the only actuator).
        try:
            st = json.loads(store.meta_get(self.conn, "stress_state") or "{}")
            lev, lceil = st.get("lev_now"), st.get("lev_ceiling")
            upd = float(st.get("updated", 0) or 0)
            if (lev is not None and lceil is not None and upd > 0
                    and time.time() - upd <= 600):
                lev, lceil = float(lev), float(lceil)
                if lceil > 0 and not math.isinf(lceil) and lev > lceil:
                    return False, (f"effective leverage {lev:.2f}x > survivable "
                                   f"ceiling {lceil:.2f}x — growth paused")
        except Exception:
            log.exception("L_eff ceiling check failed — failing open")
        floor = float(getattr(config, "MARGIN_LEVEL_STACK_FLOOR_PCT", 0) or 0)
        if floor <= 0:
            return True, ""
        try:
            blob = json.loads(store.meta_get(self.conn, "web_live") or "{}")
            lvl = blob.get("margin_level")
            updated = float(blob.get("updated", 0) or 0)
            if lvl is None or time.time() - updated > 120:
                return True, ""                    # unknown/stale — fail open
            lvl = float(lvl)
            if lvl < floor:
                self._note_stack_pause(True, lvl, floor)
                return False, f"margin level {lvl:.0f}% < stack floor {floor:.0f}%"
            self._note_stack_pause(False, lvl, floor)
            return True, ""
        except Exception:
            log.exception("stack margin check failed — failing open")
            return True, ""

    def _note_stack_pause(self, active, lvl, floor):
        """Edge-triggered pause narration (audit Wave 1): journal the seeds/rungs
        pause ONCE on entry and ONCE on exit (with duration), replacing the
        per-attempt safety spam. State is module-level (the Executor is rebuilt
        each poll cycle). This is a quiet book-state note — the escalated,
        operator-paging danger alert now comes from the price-space defense engine
        (app._run_defense). Trading behavior is UNCHANGED: the caller still pauses
        below the floor exactly as before. Never raises into the money path."""
        global _stack_pause_since
        try:
            if active and _stack_pause_since is None:
                _stack_pause_since = time.monotonic()
                self._journal("defense", "*",
                              f"seeds/rungs paused — margin level {lvl:.0f}% < stack "
                              f"floor {floor:.0f}% (see DEFENSE liq-buffer line)")
            elif not active and _stack_pause_since is not None:
                dur = time.monotonic() - _stack_pause_since
                _stack_pause_since = None
                self._journal("defense", "*",
                              f"seeds/rungs resumed — margin level {lvl:.0f}% ≥ floor "
                              f"{floor:.0f}% (paused {dur / 60:.0f}m)")
        except Exception:
            log.exception("stack-pause note failed (trade path unaffected)")

    def _respend_budget_ok(self, notional):
        """Respend-RATE throttle: a leaky bucket over new book-growth notional
        (seeds + rungs). Returns (ok, reason, debit). Call debit() ONLY after the
        order actually rests, so a post-only-killed bid never burns budget. FAILS
        OPEN: disabled, or any unreadable state, allows (operator no-blockers
        stance). Confirmed-BUY signals never reach here (respend=False). The
        bucket lives in meta so it survives the per-cycle Executor rebuild and
        restarts, same as the stack-floor's web_live read. Never raises into the
        money path."""
        rate = float(getattr(config, "RESPEND_BUDGET_USD_PER_HR", 0) or 0)
        if rate <= 0:
            return True, "", (lambda: None)                    # OFF — fail open
        try:
            burst = float(getattr(config, "RESPEND_BURST_USD", 0) or 0) or rate
            now = time.time()
            b = json.loads(store.meta_get(self.conn, "respend_bucket") or "{}")
            tokens = min(burst, float(b.get("tokens", burst))
                         + max(0.0, now - float(b.get("updated", now))) * rate / 3600.0)
            if tokens + 1e-9 < notional:
                store.meta_set(self.conn, "respend_bucket",
                               json.dumps({"tokens": tokens, "updated": now}))
                return (False, f"respend paced: bucket ${tokens:.0f} < ${notional:.0f} "
                               f"(rate ${rate:g}/hr) — letting the cushion breathe",
                        (lambda: None))

            def _debit():
                # Re-apply accrual on the debit path too. Reading the STORED tokens and
                # then stamping updated=now would DISCARD everything earned since the
                # last write and restart the clock — the bucket would refill strictly
                # slower than RESPEND_BUDGET_USD_PER_HR, tightening the throttle well
                # past what's configured (07-27 audit: 966 blocks vs 26 rungs in 13h).
                # Accrue to a fresh `then`, not the check's `now`, so the time spent
                # placing the order is credited rather than dropped.
                bb = json.loads(store.meta_get(self.conn, "respend_bucket") or "{}")
                then = time.time()
                t = min(burst, float(bb.get("tokens", burst))
                        + max(0.0, then - float(bb.get("updated", then))) * rate / 3600.0)
                store.meta_set(self.conn, "respend_bucket",
                               json.dumps({"tokens": max(0.0, t - notional), "updated": then}))
            return True, "", _debit
        except Exception:
            log.exception("respend budget check failed — failing open")
            return True, "", (lambda: None)

    def _respend_credit(self, notional, sym):
        """Refund a canceled-unfilled entry bid's notional to the respend bucket
        (operator directive 2026-07-30: a TTL/post-only re-place of an existing bid
        is not new book growth, so the churn must not drain the bucket — it was
        starving the priciest min-lot pairs, ZEC/USDC, out of ever re-seeding).
        Debit fires only once a bid rests; this is its inverse at the only terminal
        where a rested bid dies without a fill, so rest->die->re-place nets zero.
        KNOWN LOOSENESS, accepted: confirmed-BUY bids never debited (respend=False)
        but their rare TTL cancel still credits here — a phantom credit bounded by
        the burst cap, erring toward re-levering faster (operator no-blockers
        stance) rather than tracking per-row debit state in the polymorphic error
        column. Same accrue-then-write discipline as _debit: stale-stamping the
        stored tokens would discard accrual earned since the last write."""
        rate = float(getattr(config, "RESPEND_BUDGET_USD_PER_HR", 0) or 0)
        if rate <= 0 or not notional or notional <= 0:
            return
        try:
            burst = float(getattr(config, "RESPEND_BURST_USD", 0) or 0) or rate
            b = json.loads(store.meta_get(self.conn, "respend_bucket") or "{}")
            now = time.time()
            tokens = min(burst, float(b.get("tokens", burst))
                         + max(0.0, now - float(b.get("updated", now))) * rate / 3600.0)
            new = min(burst, tokens + float(notional))
            store.meta_set(self.conn, "respend_bucket",
                           json.dumps({"tokens": new, "updated": now}))
            log.info("RESPEND %s: canceled-unfilled bid refunds $%.2f to the bucket "
                     "($%.0f -> $%.0f) — a re-place is not growth", sym, notional, tokens, new)
        except Exception:
            log.exception("respend credit failed (bucket unchanged — throttle only tightens)")

    def _last_local_price(self, symbol):
        """Newest 15m candle close from our OWN store, or None. A rough, possibly
        minutes-stale price — good enough to decide whether a bid is worth pricing at
        all, and free: the point is to answer that WITHOUT a ticker call. Never used
        for sizing, stops, or anything that reaches the exchange."""
        try:
            row = self.conn.execute(
                "SELECT c FROM candles WHERE pair=? AND interval=15 ORDER BY ts DESC LIMIT 1",
                (symbol,)).fetchone()
            return float(row[0]) if row and row[0] else None
        except Exception:
            return None

    def _respend_would_refuse(self, symbol, ref_price):
        """Cheap read-only pre-gate: True when the bucket provably cannot fund even the
        SMALLEST rung this symbol could produce, so the caller can skip before spending
        a public-ticker REST call and a narration line on work that ends in a refusal.

        Conservative by construction — it compares against a true LOWER bound on the
        rung notional (min placeable volume at a price one ladder step BELOW ref_price,
        which is at or under whatever the real rung prices at), so it can never skip a
        rung that would in fact have been funded. The authoritative check remains
        _respend_budget_ok() on the real sizing. Never raises: any doubt -> False
        (proceed), matching the governor's own fail-open stance."""
        try:
            rate = float(getattr(config, "RESPEND_BUDGET_USD_PER_HR", 0) or 0)
            if rate <= 0 or not ref_price or ref_price <= 0:
                return False                                   # OFF / unknown — proceed
            info = store.get_pair_info(self.conn, symbol) or {}
            floor_price = ref_price * (1 - config.LADDER_STEP_PCT)
            vol = self._min_volume(info.get("ordermin") or 0.0, info.get("costmin") or 0.0,
                                   floor_price, info.get("lot_decimals"))
            if not vol or vol <= 0:
                return False
            burst = float(getattr(config, "RESPEND_BURST_USD", 0) or 0) or rate
            b = json.loads(store.meta_get(self.conn, "respend_bucket") or "{}")
            now = time.time()
            tokens = min(burst, float(b.get("tokens", burst))
                         + max(0.0, now - float(b.get("updated", now))) * rate / 3600.0)
            return (tokens + 1e-9) < (vol * floor_price)
        except Exception:
            log.exception("respend pre-gate failed — proceeding (fail open)")
            return False

    def place_entry(self, symbol, entry_price, card, respend=False):
        if self.mode == "off":
            return None
        try:
            return self._place_entry(symbol, entry_price, card, respend=respend)
        except Exception:
            log.exception("executor.place_entry failed for %s (never kills the writer)", symbol)
            return None

    def _place_entry(self, symbol, entry_price, card, respend=False):
        if symbol not in config.MARGIN_PAIR:
            log.error("no :BTNL margin pair for %s — cannot execute", symbol)
            return None
        ok_acc, why = self._accumulation_allowed()
        if not ok_acc:
            log.info("EXEC %s: accumulation paused (regime gate) — %s", symbol, why)
            return None
        equity = self.portfolio_value()
        ok, reason = self.rails_ok(equity)
        if not ok:
            log.warning("EXEC blocked for %s: %s", symbol, reason)
            return None
        leverage = config.PER_PAIR_LEVERAGE.get(symbol)
        if not leverage:
            log.error("no leverage for %s", symbol)
            return None
        stop = self.compute_stop(symbol, entry_price, card)
        sizing = self.size(symbol, entry_price, stop, leverage, equity, card=card)
        if sizing is None:
            log.warning("EXEC %s: sizing produced nothing (equity=%s)", symbol, equity)
            return None
        # Per-order notional ceiling (Finding 8): a checked bound on blast radius. A
        # valid min order is ~$3-8, so this only ever trips on a corrupt pairs row or a
        # flipped size mode producing an order orders-of-magnitude too large. Refuse it
        # (never halt the bot); the loud ERROR is the audit trail.
        # Conviction-SCALED (operator decision 2026-07-10): a legit Nx-conviction order
        # gets an Nx budget, so the steepened 2x/3x curve's strongest signals aren't
        # refused, while a corrupt row (orders of magnitude larger) still trips at every
        # tier. Guard on the BASE ceiling>0 so 0 still fully disables.
        cmult = sizing.get("conviction_mult", 1.0)
        # SIZE_MULT scales the notional too (audit 2026-07-13 M5): the operator's 3x
        # multiplier is a LEGITIMATE part of every order's size, so the sanity ceiling
        # must budget for it — otherwise a price rally silently trips the refuse path
        # on high-ordermin pairs (an unintended blocker). Corrupt-row protection is
        # preserved: the ceiling still catches orders orders-of-magnitude oversized.
        smult = max(1.0, float(sizing.get("size_mult", 1.0) or 1.0))
        ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult * smult
        if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
            log.error("EXEC %s REFUSED: order notional $%.2f exceeds %gx-conviction x %gx-size "
                      "ceiling $%.2f — not sending (sanity guard, not a rail; check the pairs "
                      "row / EXEC_SIZE_MODE)", symbol, sizing["notional"], cmult, smult, ceiling)
            return None

        # Respend-rate throttle (seeds/rungs only; signals pass respend=False and
        # bypass). Meters new book-growth notional so a T/P cushion re-levers over
        # days, not hours. debit() is called only once the bid definitely rests.
        debit = (lambda: None)
        if respend:
            ok_b, why_b, debit = self._respend_budget_ok(sizing["notional"])
            if not ok_b:
                log.debug("EXEC %s: %s", symbol, why_b)      # per-pair per-cycle — debug
                return None

        margin_pair = config.MARGIN_PAIR[symbol]
        tick = config.MARGIN_TICK_DECIMALS.get(symbol, 2)
        if config.ENTRY_ORDERTYPE == "limit":
            # Post-only maker BUY must not cross the ask, or Kraken rejects it and
            # the entry silently never rests. Bid just below last so it always rests.
            px = _round_price(entry_price * (1 - config.POST_ONLY_SLIP_PCT), tick)
        else:
            px = _round_price(entry_price, tick)
        stop_px = _round_price(stop, tick)
        vol = sizing["volume"]
        tag = (f" ({cmult:g}x conviction)" if cmult > 1.0
               else " (FLOORED-min)" if sizing["floored_to_min"]
               else " (CAPPED)" if sizing["capped"] else "")
        log.info("EXEC %s [%s] %.6g @ %s x%d lev · stop %s · notional $%.2f margin $%.2f risk $%.2f%s",
                 symbol, self.mode, vol, px, leverage, stop_px, sizing["notional"],
                 sizing["margin"], sizing["actual_risk"], tag)

        params = {"pair": margin_pair, "type": "buy", "ordertype": config.ENTRY_ORDERTYPE,
                  "volume": str(vol), "leverage": str(leverage)}
        if config.ENTRY_ORDERTYPE == "limit":
            params["price"] = str(px)
            params["oflags"] = "post"

        row = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": symbol, "margin_pair": margin_pair, "side": "buy",
            "ordertype": config.ENTRY_ORDERTYPE, "mode": self.mode,
            "entry": px, "stop": stop_px, "volume": vol, "leverage": leverage,
            "notional": sizing["notional"], "margin": sizing["margin"],
            "risk_usd": sizing["actual_risk"],
            # Persist the entry conviction so continuous laddering can size each
            # rung the same (the fill->rung chain flows through the DB).
            "score": getattr(card, "score", None), "required": getattr(card, "required", None),
            "txid": None, "stop_txid": None,
            "status": "pending", "error": None,
            # Client order id (audit C3): sent on the live AddOrder so an order whose
            # transport failed AMBIGUOUSLY can be re-identified on Kraken instead of
            # becoming an untracked naked position. int32-positive per Kraken spec.
            "userref": _new_userref(),
        }

        if self.mode == "paper":
            row["txid"] = f"PAPER-{int(datetime.datetime.now().timestamp())}"
            row["status"] = "open"
            oid = store.insert_order(self.conn, row)
            self._rest_stop(symbol, margin_pair, stop_px, vol, leverage, oid, paper=True)
            return oid

        if self.mode == "validate":
            params["validate"] = "true"
            vmeta = {}
            res = broker.private("/0/private/AddOrder", params, meta=vmeta)
            row["status"] = "validated" if res is not None else "rejected"
            # keep Kraken's verbatim reject so the probe can triage (precision
            # coarsening vs no-:BTNL-book drop) instead of a blind None
            row["error"] = None if res is not None else vmeta.get("error", "validate returned None")
            if res is not None:
                row["txid"] = str((res.get("descr") or {}).get("order", "validated"))
            return store.insert_order(self.conn, row)

        # live: place the post-only maker limit and record it PENDING. A resting
        # limit is NOT a position — do not rest a stop yet (a stop with no position
        # would open a short) and do not count it as open. poll_fills() promotes
        # it to 'open' and rests the protective stop only once Kraken confirms fill.
        params["userref"] = str(row["userref"])
        tmeta = {}
        res = broker.private("/0/private/AddOrder", params, idempotent=False, meta=tmeta)
        if res and res.get("txid"):
            row["txid"] = res["txid"][0]
            row["status"] = "pending"
            debit()                                  # respend budget: the bid rests
            log.info("ENTRY %s: limit resting @ %s (pending fill) %s", symbol, px, row["txid"])
            conv = f" · {cmult:g}x conviction" if cmult > 1.0 else ""
            self._journal("order", symbol, f"{row['volume']:g} entry limit resting @ {px} (pending){conv}")
            return store.insert_order(self.conn, row)
        if not tmeta.get("definite"):
            # Ambiguous transport (audit C3): the order MAY be on the book. Record it
            # pending with NO txid; poll_fills' userref recovery adopts it if it landed
            # or retires it 'rejected' once Kraken definitively shows nothing.
            row["status"] = "pending"
            row["error"] = "ambiguous AddOrder (network) — resolving by userref"
            log.warning("ENTRY %s: AddOrder transport ambiguous — recorded pending, "
                        "userref %s recovery will resolve", symbol, row["userref"])
            self._journal("order", symbol, f"ambiguous entry AddOrder — resolving by userref {row['userref']}")
            return store.insert_order(self.conn, row)
        row["status"] = "rejected"
        row["error"] = "no txid from AddOrder"
        return store.insert_order(self.conn, row)

    def poll_fills(self):
        """Promote resting entry limits to positions once Kraken confirms fill,
        then rest the protective stop. A limit sits 'pending' until this sees an
        executed volume — so pos counts, P&L, stops, and re-verification never
        touch an unfilled order. Terminal-but-unfilled orders become 'canceled'.
        LIVE only; paper simulates instant fill at placement."""
        if self.mode != "live":
            return
        # Re-protect BEFORE the T/P gate (audit 2026-07-13 M3): while an INCOMPLETE
        # flatten retries, _check_take_profit returns True every cycle — but by then
        # CancelOrderBatch has already swept the protective stops, so skipping the
        # reprotect pass here left blocked pairs naked for the whole retry period
        # (longest exactly when the Kraken API is degraded). Reprotect is cheap in
        # the healthy case (no API call when nothing is naked); the flatten simply
        # re-cancels anything it re-arms — churn, never nakedness.
        self._reprotect_naked_open()
        if self._check_take_profit():
            return          # book was just flattened — nothing to promote or ladder this cycle
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, txid, ts, entry, score, required, "
            "COALESCE(notional, entry*volume) "
            "FROM orders WHERE status='pending' AND mode=?", (self.mode,)).fetchall()
        # ONE batched QueryOrders sweep for every pending txid (50/call). On the
        # full-universe roster (2026-07-19) a healthy book RESTS 130+ seed bids —
        # per-row lookups would be 130+ private calls per poll cycle, the exact
        # per-account-budget failure the 2026-07-17 rate-limit storm proved out.
        # A txid ABSENT from the map is UNKNOWN (== query_order None): skip and
        # converge next cycle, never treat as gone.
        known = broker.query_orders([r[6] for r in rows if r[6]])
        for oid, sym, mpair, vol, lev, stop, txid, ts, entry, score, required, row_notional in rows:
            if not txid:
                # Ambiguous-AddOrder recovery (audit C3): this row was born from an
                # AddOrder whose network transport failed — it MAY be on the book.
                # Find it by our userref; adopt the txid if so, conclude 'rejected'
                # only after Kraken definitively shows nothing for the ref.
                txid = self._resolve_ambiguous_entry(oid, sym, ts)
                if not txid:
                    continue
                o = broker.query_order(txid)    # just adopted — not in the batch sweep
            else:
                o = known.get(txid)
            if o is None:
                continue                        # transient query failure — retry next cycle
            status = o.get("status")
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            if status not in ("closed", "canceled", "expired"):
                # Two reasons to act on a still-resting order: a PARTIAL fill (a real
                # position forming), or a stale UNFILLED bid past its TTL (Finding 5 —
                # else post-only bids pile up against Kraken's open-order cap). Both
                # cancel the order and resolve to its TERMINAL state before any DB
                # transition. Hazards (Finding 4): more can fill between the query and
                # the cancel landing, and the cancel itself can FAIL — so NEVER transition
                # until terminal with a settled volume (flipping to 'open' early would
                # size off a stale snapshot, or on a failed cancel orphan a still-resting
                # remainder — poll_fills only revisits 'pending'). On any failure/
                # uncertainty leave it pending and converge next cycle.
                if vol_exec > 0:
                    log.info("FILL %s: partial %.6g while resting — canceling remainder", sym, vol_exec)
                elif _entry_ttl_expired(ts):
                    log.info("EXPIRE %s: entry bid unfilled past TTL (%.0fs) — canceling stale post-only bid",
                             sym, _age_secs(ts))
                    self._journal("expire", sym, "entry bid unfilled past TTL — canceling")
                else:
                    continue                    # unfilled + resting, within TTL — patient bid
                if broker.cancel_order(txid) is None:
                    log.warning("FILL %s: cancel FAILED — leaving pending, retry next cycle", sym)
                    continue
                o = broker.query_order(txid)
                if o is None:
                    log.info("FILL %s: cancel sent — awaiting terminal confirm next cycle", sym)
                    continue
                status = o.get("status")
                if status not in ("closed", "canceled", "expired"):
                    log.info("FILL %s: cancel sent, order not terminal yet — retry next cycle", sym)
                    continue
                try:
                    vol_exec = float(o.get("vol_exec", 0) or 0)   # settled terminal volume
                except (TypeError, ValueError):
                    vol_exec = 0.0
                # fall through to the terminal handler with the settled status/vol_exec
            if vol_exec > 0:                     # terminal + filled (fully, or partial then done)
                self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?", (vol_exec, oid))
                self.conn.commit()
                log.info("FILL %s: %.6g filled — position open, resting stop", sym, vol_exec)
                self._journal("fill", sym, f"{vol_exec:.6g} filled @ {lev}x — position open")
                self._rest_stop(sym, mpair, stop, vol_exec, lev, oid, paper=False)
                # Continuous laddering: protection is secured above; now drop the next
                # rung below the fill, sized at the SAME conviction the entry carried
                # (score/required ride down the chain). Isolated — a rung failure
                # never unwinds the fill.
                self._place_ladder_rung(sym, mpair, lev, stop, entry, score, required)
            else:
                self.conn.execute("UPDATE orders SET status='canceled', error=? WHERE id=?",
                                  (f"entry {status}, unfilled", oid))
                self.conn.commit()
                log.info("ENTRY %s: %s unfilled — no position", sym, status)
                # The bid rested (debited) and died unfilled — hand its claim back so
                # the TTL/post-only re-place cycle doesn't drain the bucket (2026-07-30).
                self._respend_credit(row_notional, sym)
        # Runtime safety nets: re-protect any 'open' position whose stop-rest failed
        # (again — fresh fills this cycle can be naked; the pre-T/P pass above covers
        # the flatten-retry window), re-place the next ladder rung for any chain whose
        # resting bid died, then seed a starter chain on any SEED_PAIRS symbol with
        # nothing working.
        self._reprotect_naked_open()
        self._check_rung_harvest()       # per-rung T/P: bank +4% rungs, stops re-arm on abort
        self._reverse_gear()             # deleverage governor (Wave 4) — sheds before it grows
        self._ensure_ladder_rungs()
        self._seed_chains()
        # Runtime exchange-truth sweep (audit 2026-07-13 #1): re-run the full
        # ledger↔Kraken reconcile on a timer so intraday stop-fires, force-
        # liquidations, and manual closes are noticed in minutes, not at restart.
        global _recon_next
        recon_secs = float(getattr(config, "RUNTIME_RECON_SECS", 0) or 0)
        if recon_secs > 0 and time.monotonic() >= _recon_next:
            _recon_next = time.monotonic() + recon_secs
            try:
                self.verify_open_stops(context="runtime")
            except Exception:
                log.exception("runtime reconcile sweep failed (poll_fills unaffected)")

    def _resolve_ambiguous_entry(self, oid, sym, ts):
        """A 'pending' row with NO txid came from an AddOrder whose transport failed
        ambiguously. Look it up by userref: found -> adopt the txid (the order IS
        ours, on the book or already terminal); definitively absent after the grace
        window -> 'rejected' (it never landed); API unknown -> retry next cycle.
        Returns the adopted txid or None."""
        row = self.conn.execute("SELECT userref FROM orders WHERE id=?", (oid,)).fetchone()
        userref = row[0] if row else None
        if not userref:
            # Legacy ambiguous row (pre-userref) — nothing to search by; age it out.
            if _age_secs(ts) > _USERREF_RESOLVE_SECS:
                self.conn.execute("UPDATE orders SET status='rejected', "
                                  "error='ambiguous AddOrder, no userref to recover by' WHERE id=?",
                                  (oid,))
                self.conn.commit()
            return None
        txid, od = broker.find_order_by_userref(userref)
        if txid:
            self.conn.execute("UPDATE orders SET txid=? WHERE id=?", (txid, oid))
            self.conn.commit()
            log.warning("RECOVER %s: ambiguous AddOrder found on Kraken by userref %s -> %s",
                        sym, userref, txid)
            self._journal("order", sym, f"recovered ambiguous entry by userref -> {txid}")
            return txid
        if od == "unknown":
            return None                          # API couldn't answer — retry next cycle
        if _age_secs(ts) > _USERREF_RESOLVE_SECS:
            self.conn.execute("UPDATE orders SET status='rejected', "
                              "error='AddOrder never landed (userref not found)' WHERE id=?", (oid,))
            self.conn.commit()
            log.info("RECOVER %s: userref %s definitively absent — AddOrder never landed", sym, userref)
        return None

    def _reprotect_naked_open(self):
        """Runtime safety net (gap B): a fill whose protective stop-rest FAILED
        (poll_fills commits 'open' then _rest_stop errors on a broker/API failure)
        stays status='open' with stop_txid NULL and is otherwise never revisited until
        a manual restart — a naked leveraged long. Re-rest it each poll cycle. Adopt a
        stop already resting on Kraken (persist-race orphan) before placing, so a retry
        never doubles the stop. Isolated — never raises into poll_fills. No API call in
        the healthy case (early-returns when nothing is naked)."""
        if self.mode != "live" or not config.PROTECTIVE_STOP:
            return
        try:
            # close_txid rows are excluded: the T/P flatten owns them — a resting
            # limit close IS the pair's exit, and re-arming a stop beside it would
            # double the sell volume (stop fires + close fills -> short).
            # COALESCE(stop_prot, stop): a rung that proved the harvest target carries a
            # ratcheted protective level (breakeven) — re-arm THERE, not at the original
            # invalidation level ~8% lower. NULL stop_prot (never ratcheted) -> `stop`.
            naked = self.conn.execute(
                "SELECT id, symbol, margin_pair, volume, leverage, "
                "COALESCE(stop_prot, stop) FROM orders "
                "WHERE status='open' AND stop_txid IS NULL AND close_txid IS NULL "
                "AND mode=?", (self.mode,)).fetchall()
            if not naked:
                return
            kr_open_orders = broker.open_orders()
            claimed = {t for (t,) in self.conn.execute(
                "SELECT stop_txid FROM orders WHERE stop_txid IS NOT NULL")}
            # Backing check (audit 2026-07-13 C2): a naked row whose position has since
            # DIED (force-liquidation, manual close) must not get a fresh stop — that
            # MANUFACTURES the orphan stop that opens a short on trigger. Rest a stop
            # only when the pair's live long volume covers this row on top of the
            # volume its sibling rows' resting stops already commit. Definite state
            # only: OpenPositions None -> skip this cycle (retry ~15s), never guess.
            kr_pos = broker.open_positions()
            if kr_pos is None:
                log.warning("REPROTECT: OpenPositions unavailable — cannot verify backing, "
                            "retry next cycle")
                return
            positions = list(kr_pos.values()) if isinstance(kr_pos, dict) else []
            rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}

            def _pair_long_vol(sym):
                key = rest_by_ws.get(sym, "")
                tot = 0.0
                for p in positions:
                    if _norm_pair_key(p.get("pair", "")) != key:
                        continue
                    if str(p.get("type", "")).lower() == "sell":
                        continue
                    try:
                        tot += max(0.0, float(p.get("vol", 0) or 0) - float(p.get("vol_closed", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                return tot

            stopped_vol = {}   # sym -> volume already committed by sibling resting stops
            for s, v in self.conn.execute(
                    "SELECT symbol, COALESCE(volume,0) FROM orders "
                    "WHERE status='open' AND stop_txid IS NOT NULL AND mode=?", (self.mode,)):
                stopped_vol[s] = stopped_vol.get(s, 0.0) + float(v or 0)
            for oid, sym, mpair, vol, lev, stop in naked:
                volf = float(vol or 0)
                free_backing = _pair_long_vol(sym) - stopped_vol.get(sym, 0.0)
                if free_backing < volf - 1e-8:
                    log.warning("REPROTECT %s: naked row %d NOT backed by live volume "
                                "(free %.8g < %.8g) — position likely gone; leaving for "
                                "the reconcile sweep to retire", sym, oid, free_backing, volf)
                    self._journal("stop", sym, f"naked row {oid} unbacked — reconcile sweep will retire it")
                    continue
                adopt = self._find_adoptable_stop(kr_open_orders, claimed, mpair, volf)
                if adopt:
                    self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (adopt, oid))
                    self.conn.commit()
                    claimed.add(adopt)
                    stopped_vol[sym] = stopped_vol.get(sym, 0.0) + volf
                    log.warning("REPROTECT %s: adopted resting orphan stop %s for naked open row %d",
                                sym, adopt, oid)
                    self._journal("stop", sym, f"reprotect: adopted resting stop {adopt}")
                    continue
                log.warning("REPROTECT %s: open row %d unprotected (stop-rest failed earlier) — "
                            "resting stop now", sym, oid)
                self._rest_stop(sym, mpair, stop, vol, lev, oid, paper=False)
                stopped_vol[sym] = stopped_vol.get(sym, 0.0) + volf
        except Exception:
            log.exception("reprotect pass failed (poll_fills unaffected)")

    def _live_last(self, symbol):
        """Last trade price from the public REST ticker, or None. Used only to keep
        a post-only rung below the live market — never for sizing or stops."""
        rp = _REST_PAIR.get(symbol)
        if not rp:
            return None
        try:
            res = rest_client.fetch_ticker([rp])
            if not res:
                return None
            t = next(iter(res.values()), None)
            return float(t["c"][0]) if t else None
        except Exception:
            log.exception("LADDER %s: live ticker fetch failed (rung falls back to "
                          "fill-anchored price)", symbol)
            return None

    def _ensure_ladder_rungs(self):
        """Runtime safety net (2026-07-12 offline-gap incident): the fill→rung
        accumulation chain dies SILENTLY when a resting rung goes terminal unfilled —
        a post-only kill on a gap-down (Kraken cancels the crossing maker, reason
        'Post only order') or the entry-TTL sweep — because nothing re-placed it; the
        pair then sat with open positions and ZERO resting bid until its next daily
        close (a 12h offline gap left every confirmed-BUY pair bidless in a falling
        market). Each poll cycle: any LIVE symbol with open rows but no pending entry
        gets its next rung re-placed, anchored one step below the LOWEST open fill
        (the chain's natural continuation; the live clamp in _place_ladder_rung keeps
        the maker resting after a further gap-down). Every rung guard still applies
        (HALT, regime gate, stop floor, own-level-once, rails). A symbol whose attempt
        doesn't rest a bid backs off _RELADDER_RETRY_SECS so a chain holding at its
        ladder floor stays quiet. No API call when every chain has a resting bid.
        Isolated — never raises into poll_fills."""
        if self.mode != "live" or not config.LADDER_CONTINUOUS:
            return
        try:
            rows = self.conn.execute(
                "SELECT symbol, margin_pair, entry, stop, leverage, score, required "
                "FROM orders WHERE status='open' AND mode=? AND entry IS NOT NULL "
                "ORDER BY symbol, entry ASC", (self.mode,)).fetchall()
            lowest = {}
            for sym, mpair, entry, stop, lev, score, required in rows:
                lowest.setdefault(sym, (mpair, entry, stop, lev, score, required))
            now = time.monotonic()
            for sym, (mpair, entry, stop, lev, score, required) in lowest.items():
                if store.has_pending_entry(self.conn, sym, mode=self.mode):
                    continue
                if now < _reladder_next.get(sym, 0.0):
                    continue
                # Respend pre-gate BEFORE the retry-clock stamp and before any work: a
                # paced bucket otherwise burned a public-ticker REST call plus a RELADDER
                # + LADDER narration pair per pair per pass, only to be refused at the
                # last step (07-27 audit: ~27 pairs x every 10min). Leaving the clock
                # unstamped means the pair retries as soon as the bucket refills instead
                # of eating a full backoff for a refusal that never reached the exchange.
                if self._respend_would_refuse(sym, entry):
                    log.debug("RELADDER %s: respend bucket can't fund the smallest rung "
                              "— skipping before the ticker fetch", sym)
                    continue
                _reladder_next[sym] = now + _RELADDER_RETRY_SECS
                # Stays INFO: past the pre-gate this fires only when a rung will really be
                # attempted, which is the self-heal this safety net exists to surface.
                log.info("RELADDER %s: open chain with no resting bid — re-placing "
                         "next rung below lowest open fill %s", sym, entry)
                self._journal("order", sym, f"reladder: chain had no resting bid — "
                                            f"re-placing rung below lowest fill {entry:g}")
                self._place_ladder_rung(sym, mpair, lev, stop, entry, score, required)
        except Exception:
            log.exception("reladder pass failed (poll_fills unaffected)")

    def _seed_chains(self):
        """Operator all-pairs directive (2026-07-13): every config.SEED_PAIRS symbol
        keeps a ladder chain WORKING at all times. A pair with NO open rows and NO
        resting bid gets a starter chain: a post-only bid just below live, through
        the exact _place_entry path a confirmed BUY uses (regime gate, HALT via
        rails_ok, min x SIZE_MULT sizing, pct-clamped stop — card=None so no
        conviction). This is also what RESTACKS the book after a T/P flatten.
        Backs off _RELADDER_RETRY_SECS per symbol so a rejecting pair stays quiet.
        Isolated — never raises into poll_fills."""
        if self.mode != "live" or not getattr(config, "SEED_PAIRS", ()):
            return
        # Margin-stack floor (audit #2): seeds GROW the book — below the floor, stop
        # growing (signal entries stay untouched; fails open on stale/unknown level).
        ok_ml, ml_why = self._stack_margin_ok()
        if not ok_ml:
            log.info("SEED: paused — %s", ml_why)
            return
        try:
            now = time.monotonic()
            for sym in config.SEED_PAIRS:
                if now < _seed_next.get(sym, 0.0):
                    continue
                if store.has_pending_entry(self.conn, sym, mode=self.mode):
                    continue
                if self.conn.execute("SELECT 1 FROM orders WHERE symbol=? AND status='open' "
                                     "AND mode=? LIMIT 1", (sym, self.mode)).fetchone():
                    continue
                # Respend pre-gate, same as the reladder path and for the same three
                # reasons — this is its twin and was missed when that one was fixed
                # (07-27 watch): a paced bucket otherwise stamps a full backoff, burns a
                # public-ticker REST call, and narrates "starting ladder with a post-only
                # bid" at INFO before being refused at DEBUG. The dangling announcement is
                # worse than the spam it replaced: the log reads as a placement that
                # vanished. Priced off our OWN candles so the check itself costs nothing.
                if self._respend_would_refuse(sym, self._last_local_price(sym)):
                    log.debug("SEED %s: respend bucket can't fund the smallest bid "
                              "— skipping before the ticker fetch", sym)
                    continue
                _seed_next[sym] = now + _RELADDER_RETRY_SECS
                live = self._live_last(sym)
                if not live or live <= 0:
                    continue
                log.info("SEED %s: no chain working — starting ladder with a post-only "
                         "bid below live %s", sym, live)
                self._journal("order", sym, f"seed: starting chain below live {live:g}")
                self.place_entry(sym, live, card=None, respend=True)
        except Exception:
            log.exception("seed pass failed (poll_fills unaffected)")

    # ── reverse gear: deleverage governor (Wave 4) ───────────────────────────

    def _liq_buffer(self, bal):
        """Price-space liq buffer % from a live TradeBalance (e, m, v), via the
        shared defense math (ONE definition, so the governor and the Wave-1
        telemetry can never disagree). None on unknown/bad read."""
        from . import defense
        if not bal:
            return None
        try:
            e = broker.equity(bal)
            m = float(bal.get("m") or 0)
            v = float(bal.get("v") or 0)
        except (TypeError, ValueError):
            return None
        c = defense.compute(e, m, v)
        return c.get("buffer_liq_pct") if c else None

    def _pair_net_long(self, sym):
        """Live net long volume for a pair from OpenPositions (long lots' vol minus
        vol_closed; shorts ignored). None on API FAILURE (never guess — a failed read
        must not be read as 'nothing to back the close', which would sell blind)."""
        key = _REST_PAIR.get(sym, "")
        kr = broker.open_positions()
        if kr is None:
            return None
        tot = 0.0
        for p in (kr.values() if isinstance(kr, dict) else []):
            if _norm_pair_key(p.get("pair", "")) != key:
                continue
            if str(p.get("type", "")).lower() == "sell":
                continue
            try:
                tot += max(0.0, float(p.get("vol", 0) or 0) - float(p.get("vol_closed", 0) or 0))
            except (TypeError, ValueError):
                continue
        return tot

    def _pick_trim_lot(self):
        """The next whole fill-lot to shed: the largest-notional OPEN row (sheds the
        most exposure per close -> fewest closes, least fee drag). Rows closed earlier
        in this pass are already status!='open', so they self-exclude. None when flat."""
        row = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop_txid, "
            "COALESCE(notional,0) FROM orders WHERE status='open' AND mode=? "
            "ORDER BY COALESCE(notional,0) DESC, volume DESC LIMIT 1", (self.mode,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "symbol": row[1], "margin_pair": row[2], "volume": row[3],
                "leverage": row[4], "stop_txid": row[5], "notional": row[6]}

    def _close_lot(self, lot):
        """Close ONE whole fill-lot: cancel its stop, market-close its long volume
        (capped at the pair's live net long so it can NEVER flip short), retire the
        row. The per-lot primitive of the reverse gear — closes a whole DB row exactly
        as the bot models it, so no stop is ever left oversized or partially naked.
        Returns True on a confirmed close. On a FAILED close it NULLs the row's
        stop_txid so _reprotect_naked_open re-arms a stop next cycle (never leaves a
        silently-naked lot). Never raises."""
        oid = lot["id"]; sym = lot["symbol"]; mpair = lot["margin_pair"]
        lev = lot["leverage"]; stx = lot["stop_txid"]
        try:
            vol = float(lot["volume"] or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol <= 0:
            self.conn.execute("UPDATE orders SET status='closed', error='reverse-gear (zero vol)' WHERE id=?", (oid,))
            self.conn.commit()
            return True
        net = self._pair_net_long(sym)
        if net is None:
            log.warning("REVERSE-GEAR %s: net long unavailable — not closing lot %d blind", sym, oid)
            return False
        info = store.get_pair_info(self.conn, sym) or {}
        lot_dec = info.get("lot_decimals")
        close_vol = _round_down(min(vol, net), lot_dec) if lot_dec is not None else min(vol, net)
        if close_vol <= 0:
            # position already gone/flat for this pair — retire the row, drop its stop
            if stx:
                broker.cancel_order(stx)
            self.conn.execute("UPDATE orders SET status='closed', stop_txid=NULL, error='reverse-gear (no backing)' WHERE id=?", (oid,))
            self.conn.commit()
            return True
        volstr = f"{close_vol:.{lot_dec}f}" if lot_dec is not None else f"{close_vol:.8f}"
        if stx:                                   # cancel this lot's whole stop first (no partial resize)
            broker.cancel_order(stx)
        res = broker.private("/0/private/AddOrder",
                             {"pair": mpair, "type": "sell", "ordertype": "market",
                              "volume": volstr, "leverage": str(lev)}, idempotent=False)
        if res and res.get("txid"):
            self.conn.execute("UPDATE orders SET status='closed', stop_txid=NULL, error='reverse-gear trim' WHERE id=?", (oid,))
            self.conn.commit()
            log.warning("REVERSE-GEAR %s: closed lot %d (%s) %s", sym, oid, volstr, res["txid"][0])
            self._journal("reverse-gear", sym, f"trim: market-closed {volstr} (lot {oid})")
            return True
        # close FAILED — the stop is already canceled, so NULL it in the DB too and let
        # _reprotect_naked_open re-arm a stop next cycle (never a silently-naked lot).
        self.conn.execute("UPDATE orders SET stop_txid=NULL, error='reverse-gear close failed' WHERE id=?", (oid,))
        self.conn.commit()
        log.error("REVERSE-GEAR %s: market close FAILED for lot %d — stop dropped, reprotect re-arms next cycle", sym, oid)
        self._journal("reverse-gear", sym, f"trim close FAILED lot {oid} — reprotect will re-arm")
        return False

    def _cancel_resting_bids(self):
        """On a reverse-gear fire, cancel every resting entry bid so a fill can't
        re-lever the book mid-deleverage (the 2026-07-16 incident: ladder bids kept
        filling on the exchange and grew the book back while it was being trimmed).
        Best-effort, isolated."""
        oo = broker.open_orders()
        if oo is None:
            return 0
        n = 0
        for txid, o in oo.items():
            if (o.get("descr") or {}).get("type") == "buy":
                broker.cancel_order(txid)
                n += 1
        if n:
            self.conn.execute("UPDATE orders SET status='canceled', "
                              "error='reverse-gear: bid canceled to stop re-lever' WHERE status='pending' AND mode=?",
                              (self.mode,))
            self.conn.commit()
            self._journal("reverse-gear", "*", f"canceled {n} resting bid(s) to prevent re-lever")
        return n

    def _reverse_gear(self):
        """DELEVERAGE GOVERNOR. When the price-space liq buffer decays below
        REVERSE_GEAR_TRIGGER_PCT, shed whole fill-lots (largest notional first) until
        it recovers to REVERSE_GEAR_TARGET_PCT — event-triggered market closes of open
        long volume (capped at net long, never a resting sell, never net short). Per
        LOT so a stop can never oversize/naked. FAIL-OPEN: unknown/flat buffer never
        trims. Capped per pass (a bad read can't flatten the book). LIVE + armed only.
        Isolated — never raises into poll_fills."""
        from . import defense
        if self.mode != "live" or not getattr(config, "REVERSE_GEAR_ENABLED", False):
            return
        try:
            trigger = float(getattr(config, "REVERSE_GEAR_TRIGGER_PCT", 8.0) or 8.0)
            target = float(getattr(config, "REVERSE_GEAR_TARGET_PCT", 12.0) or 12.0)
            max_lots = int(getattr(config, "REVERSE_GEAR_MAX_LOTS_PER_PASS", 4) or 4)
            start = self._liq_buffer(broker.trade_balance_full())
            if not defense.should_trim(start, trigger):
                return                               # healthy / unknown / flat — never trim blind
            log.warning("REVERSE-GEAR ARMED: liq buffer %.1f%% < trigger %.0f%% — deleveraging to %.0f%%",
                        start, trigger, target)
            if getattr(config, "REVERSE_GEAR_CANCEL_BIDS", True):
                self._cancel_resting_bids()          # stop re-lever before shedding
            closed = []
            for _ in range(max_lots):
                buf = self._liq_buffer(broker.trade_balance_full())
                if buf is None or buf >= target:
                    break
                lot = self._pick_trim_lot()
                if not lot:
                    log.warning("REVERSE-GEAR: buffer %.1f%% still < target but no lots left to shed", buf)
                    break
                if not self._close_lot(lot):
                    break                            # a close failed — stop, re-evaluate next cycle
                closed.append(lot["symbol"])
            if closed:
                end = self._liq_buffer(broker.trade_balance_full())
                self._safety("reverse-gear", "*",
                             f"deleveraged: liq buffer {start:.1f}% < trigger {trigger:.0f}% — shed "
                             f"{len(closed)} lot(s) [{', '.join(closed)}] -> buffer "
                             f"{end:.1f}%" if end is not None else
                             f"deleveraged: shed {len(closed)} lot(s) [{', '.join(closed)}]")
        except Exception:
            log.exception("reverse gear failed (poll_fills unaffected)")

    def _cancel_pair_bids(self, sym):
        """Cancel this pair's resting entry bids only (the symbol-scoped sibling of
        _cancel_resting_bids). An operator trim sheds ONE line; canceling the whole
        book's bids would be collateral damage, but leaving THIS pair's rungs resting
        would let the ladder refill what was just shed. Best-effort, isolated."""
        key = _REST_PAIR.get(sym, "")
        oo = broker.open_orders()
        if oo is None:
            log.warning("TRIM %s: open-orders read failed — bids left resting", sym)
            return 0
        n = 0
        for txid, o in oo.items():
            d = o.get("descr") or {}
            if d.get("type") != "buy" or _norm_pair_key(d.get("pair", "")) != key:
                continue
            broker.cancel_order(txid)
            n += 1
        if n:
            self.conn.execute("UPDATE orders SET status='canceled', "
                              "error='operator trim: bid canceled to stop refill' "
                              "WHERE status='pending' AND mode=? AND symbol=?", (self.mode, sym))
            self.conn.commit()
            self._journal("trim", sym, f"canceled {n} resting bid(s) so the ladder can't refill")
        return n

    def trim_pair(self, sym, max_lots=None):
        """OPERATOR TRIM: shed a named pair's open lots on demand — the manual
        counterpart to _reverse_gear, which only fires off the liq buffer and picks
        lots book-wide. Same per-lot primitive (_close_lot: cancel stop -> market
        close capped at live net long -> retire row), so a trim can never leave an
        oversized stop, a naked lot, or a net short.

        RUN THIS ONLY WITH THE LIVE BOT STOPPED. Kraken's nonce is per API key and
        the rate counter is per ACCOUNT: a second process on the same key collides
        nonces and throttles the running bot blind (2026-07-19 incident).

        Largest notional first; max_lots=None sheds the whole line. Returns a summary
        dict. Never raises."""
        out = {"symbol": sym, "closed": 0, "failed": 0, "bids_canceled": 0,
               "ml_before": None, "ml_after": None}
        if self.mode != "live":
            log.error("TRIM %s: EXEC_MODE=%s — live only", sym, self.mode)
            return out
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop_txid, "
            "COALESCE(notional,0) FROM orders WHERE status='open' AND mode=? AND symbol=? "
            "ORDER BY COALESCE(notional,0) DESC, volume DESC", (self.mode, sym)).fetchall()
        if max_lots is not None:
            rows = rows[:max_lots]
        if not rows:
            log.warning("TRIM %s: no open lots", sym)
            return out
        out["ml_before"] = self._margin_level(broker.trade_balance_full())
        log.warning("TRIM %s: operator-requested — shedding %d lot(s), margin level %s",
                    sym, len(rows), f"{out['ml_before']:.0f}%" if out["ml_before"] else "unknown")
        out["bids_canceled"] = self._cancel_pair_bids(sym)   # stop the refill before shedding
        for r in rows:
            lot = {"id": r[0], "symbol": r[1], "margin_pair": r[2], "volume": r[3],
                   "leverage": r[4], "stop_txid": r[5], "notional": r[6]}
            if self._close_lot(lot):
                out["closed"] += 1
            else:
                out["failed"] += 1
                log.error("TRIM %s: lot %d failed — stopping (reprotect re-arms next cycle)",
                          sym, lot["id"])
                break                    # a failed close means stale state; don't keep selling
        out["ml_after"] = self._margin_level(broker.trade_balance_full())
        msg = f"operator trim {sym}: shed {out['closed']} lot(s)"
        if out["failed"]:
            msg += f", {out['failed']} FAILED"
        if out["ml_before"] is not None and out["ml_after"] is not None:
            msg += f" — margin level {out['ml_before']:.0f}% -> {out['ml_after']:.0f}%"
        self._safety("trim", sym, msg)
        return out

    def _margin_level(self, bal):
        """Margin level % (equity / used margin) from a live TradeBalance. None on a
        bad/unknown read or a flat book — never a fabricated 0."""
        if not bal:
            return None
        try:
            e = broker.equity(bal)
            m = float(bal.get("m") or 0)
        except (TypeError, ValueError):
            return None
        if e is None or m <= 0:
            return None
        return 100.0 * e / m

    # ── equity take-profit (operator 2026-07-13) ─────────────────────────────

    def _check_take_profit(self):
        """Stack, then flatten EVERYTHING at +TP_PCT over the armed baseline, then
        restack from the new base. The trigger rides the existing poll cycle; the
        closes are immediate market orders sized to Kraken's LIVE open volume at
        that instant — never a resting sell, so the no-net-short rule stands.
        Baseline lives in meta['tp_baseline']: armed at first sight of live equity,
        reset to post-flatten equity after each cycle (compounding). Returns True
        when a flatten ran (caller skips the rest of its cycle). Failure shape: an
        unavailable exchange read aborts BEFORE anything is touched and the trigger
        simply re-fires next poll; a partial flatten leaves unclosed rows protected
        (stops intact) or reprotectable (stop_txid NULL -> _reprotect_naked_open).
        Isolated — never raises into poll_fills."""
        global _tp_trough_noted
        if self.mode != "live" or not getattr(config, "TP_ENABLED", False):
            return False
        try:
            # A started flatten OWNS the book until every pair is confirmed flat
            # (limit closes rest/chase across polls now — operator 2026-07-21). The
            # flag decouples the retry from the equity target: without it, a price
            # drop below target mid-chase would strand half-flattened pairs with
            # sells resting and stops canceled, forever.
            flatten_active = str(store.meta_get(self.conn, "tp_flatten_active", "") or "") == "1"
            eq = self.portfolio_value()
            if eq is None and not flatten_active:
                return False
            try:
                baseline = float(store.meta_get(self.conn, "tp_baseline", 0) or 0)
            except (TypeError, ValueError):
                baseline = 0.0
            if not flatten_active:
                if baseline <= 0:
                    if eq is None:
                        return False
                    store.meta_set(self.conn, "tp_baseline", eq)
                    store.meta_set(self.conn, "tp_trough", eq)
                    store.meta_set(self.conn, "tp_cycle_flows", 0.0)
                    _tp_trough_noted = None
                    log.warning("T/P ARMED: baseline $%.2f — flatten-everything target $%.2f (+%.0f%%)",
                                eq, eq * (1 + config.TP_PCT), config.TP_PCT * 100)
                    self._journal("tp", "*", f"T/P armed: baseline ${eq:.2f}, "
                                             f"target ${eq * (1 + config.TP_PCT):.2f}")
                    return False
                # Trough ratchet (operator 2026-07-24): the target used to stay
                # parked at baseline*(1+TP_PCT) through any drawdown — unreachable
                # for months while fees bleed (audit 2026-07-13 #2). Track the
                # equity low since arm and fire off min(baseline, trough) instead.
                # The baseline itself is NEVER ratcheted: the cycle ledger keeps
                # booking TRUE profit against it (a trough-fire below baseline
                # books an honest red cycle), and the flow poller's CAS on
                # tp_baseline keeps its "changed underneath = cycle completed"
                # meaning. Ratchet is skipped while a flatten owns the book —
                # mid-chase equity is half-settled noise, not a trading trough.
                # Baseline floor (operator 2026-07-27): the ratchet may lower the
                # target toward a bounce off the low but NEVER below baseline, so a
                # fire can never settle at a loss (worst case is breakeven). Both the
                # fire check and the ratchet narration read the same _target_for so
                # they can't disagree. TP_TARGET_FLOOR_BASELINE=False restores the old
                # min(baseline,trough) behavior that could bank red cycles.
                def _target_for(tr):
                    t = min(baseline, tr) * (1 + config.TP_PCT)
                    if getattr(config, "TP_TARGET_FLOOR_BASELINE", False):
                        t = max(baseline, t)
                    return t
                trough = 0.0
                try:
                    trough = float(store.meta_get(self.conn, "tp_trough", 0) or 0)
                except (TypeError, ValueError):
                    pass
                if trough <= 0 or trough > baseline:
                    trough = baseline       # pre-ratchet meta, or clamp after a shift
                if eq < trough - 0.005:
                    trough = eq
                    store.meta_set(self.conn, "tp_trough", eq)
                    if _tp_trough_noted is None or trough <= _tp_trough_noted * 0.99:
                        _tp_trough_noted = trough
                        log.warning("T/P trough ratchet: equity low $%.2f — target now "
                                    "$%.2f (baseline $%.2f stays the profit yardstick)",
                                    trough, _target_for(trough), baseline)
                        self._journal("tp", "*", f"trough ratchet: ${trough:.2f} — "
                                                 f"target ${_target_for(trough):.2f}")
                base_eff = min(baseline, trough)
                target = _target_for(trough)
                if eq < target:
                    return False
                log.warning("T/P HIT: equity $%.2f >= target $%.2f (baseline $%.2f · trough $%.2f) — "
                            "FLATTENING THE BOOK (post-only limit closes)", eq, target, baseline, base_eff)
                self._journal("tp", "*", f"T/P HIT: ${eq:.2f} >= ${target:.2f} — flattening")
                store.meta_set(self.conn, "tp_flatten_active", "1")
            ran, complete = self._flatten_all()
            if not ran:
                return False        # exchange state unavailable — re-trigger next poll
            if not complete:
                # Limit closes are resting/chasing. The flag keeps this pass re-firing
                # every poll regardless of where equity sits, until all-flat.
                log.info("T/P: flatten in progress — limit closes resting/chasing, "
                         "next pass on the next poll")
                return True
            new_eq = self.portfolio_value()
            if new_eq is None:
                # All-flat but the settle read failed — retry next poll rather than
                # booking a cycle row off a guess; the flatten pass is a no-op now.
                log.warning("T/P: book flat but equity read failed — settling next poll")
                return True
            try:
                flows = float(store.meta_get(self.conn, "tp_cycle_flows", 0) or 0)
            except (TypeError, ValueError):
                flows = 0.0
            profit = new_eq - baseline
            note = "limit flatten"
            try:
                _tr = float(store.meta_get(self.conn, "tp_trough", 0) or 0)
                if 0 < _tr < baseline - 0.005:
                    note = f"limit flatten · fired off trough ${_tr:.2f}"
            except (TypeError, ValueError):
                pass
            try:
                store.tp_cycle_add(
                    self.conn, datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    baseline, new_eq, flows, profit, note=note)
            except Exception:
                log.exception("T/P ledger write failed (cycle still completes)")
            store.meta_set(self.conn, "tp_baseline", new_eq)
            store.meta_set(self.conn, "tp_trough", new_eq)
            store.meta_set(self.conn, "tp_cycle_flows", 0.0)
            store.meta_set(self.conn, "tp_flatten_active", "0")
            # Kill-switch peak rides the cycle (rails re-arm 2026-07-30): settled
            # profit is BANKED — off the table, not at risk. Carrying the
            # pre-flatten peak across a settle would read the deliberate flatten
            # itself as a drawdown and latch the rails through the restack.
            # peak_equity re-ratchets from here via _update_peak.
            store.meta_set(self.conn, "peak_equity", new_eq)
            _tp_trough_noted = None
            log.warning("T/P CYCLE COMPLETE: baseline $%.2f -> settled $%.2f · trading "
                        "profit $%+.2f (external flows $%+.2f rode along) — seeder "
                        "restacks next cycle", baseline, new_eq, profit, flows)
            self._journal("tp", "*", f"cycle complete: banked ${profit:+.2f} trading profit "
                                     f"(flows ${flows:+.2f}) — new baseline ${new_eq:.2f}, restacking")
            return True
        except Exception:
            log.exception("take-profit check failed (poll_fills continues)")
            return False

    def _flatten_all(self):
        """Close the whole book NOW. Safety rule (verify_open_stops' rule): act only
        on DEFINITE exchange state, and never let a sell exceed live long volume.
        Sequence:
          1. OpenOrders (None -> abort untouched): cancel every resting entry bid,
             then every resting protective stop. A stop-cancel FAILURE marks its
             pair blocked (still protected, skipped this pass); a stop already off
             the book just gets its ledger reference cleared. Cleared/canceled
             stop_txids are NULLed + committed immediately, so a crash mid-flatten
             leaves rows that _reprotect_naked_open re-arms — protected, never naked.
          2. OpenPositions FRESH (after stop-cancels — with no OTHER sells resting,
             per-pair long volume can only shrink via our own close order): rest a
             POST-ONLY LIMIT close (operator 2026-07-21 — market closes paid taker
             fees + spread) one tick over best bid for each unblocked pair's live
             volume, rounded DOWN to the lot grid (an error leaves dust-LONG, never
             short). Sized from the EXCHANGE's volume, not the ledger's. Later
             passes manage the resting close: filled -> retire rows; market fell
             away -> cancel and re-peg (a maker-only chase). Rows carry the close's
             txid in close_txid — reprotect and the reconciler leave those alone.
          3. Rows of a pair confirmed FLAT -> status='closed'.
        Returns (ran, complete): ran=False means exchange state was unavailable and
        nothing was recorded (retry next poll; a CancelAll may already have landed,
        in which case the stops-gone window lasts until a pass completes —
        tolerable seconds-to-minutes at +20% equity, and every later pass
        re-converges from definite state). complete=False means at least one pair
        is still open (blocked or a failed close) — the CALLER MUST KEEP THE
        BASELINE so the trigger re-fires and this retries what's left."""
        # 1) ONE targeted CancelOrderBatch sweeps every resting order WE placed — bids
        # and stops — instead of ~2N serial CancelOrder calls grinding the private-API
        # rate limiter. Targeted, NOT the account-wide CancelAll it used to be (audit
        # 2026-07-13 M4: CancelAll also swept manual/other-system orders, and stripped
        # the stop off any Kraken position the ledger had no row for — permanently
        # naked, since the flatten only closes DB-known pairs). Best-effort: the
        # reconcile below works from DEFINITE post-sweep state either way.
        ours = [t for (t,) in self.conn.execute(
            "SELECT txid FROM orders WHERE status='pending' AND txid IS NOT NULL AND mode=?",
            (self.mode,))]
        ours += [t for (t,) in self.conn.execute(
            "SELECT stop_txid FROM orders WHERE status='open' AND stop_txid IS NOT NULL "
            "AND mode=?", (self.mode,))]
        broker.cancel_order_batch(ours)
        oo = broker.open_orders()
        if oo is None:
            log.warning("T/P: OpenOrders unavailable after cancel sweep — cannot reconcile; "
                        "retry next poll")
            return False, False
        by_sym = {}
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, leverage, stop_txid, close_txid FROM orders "
            "WHERE status='open' AND mode=? ORDER BY symbol, id", (self.mode,)).fetchall()
        for oid, sym, mpair, lev, stop_txid, close_txid in rows:
            d = by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "closes": set(), "blocked": False})
            d["rows"].append(oid)
            if stop_txid:
                d["stops"].add(stop_txid)
            if close_txid:
                d["closes"].add(close_txid)
        # 1a) entry bids: resolve each to its TERMINAL state (batch query). A bid
        # that PARTIALLY filled before the sweep is a real long — promote it so its
        # pair's close (sized from live volume) retires it with the rest; leave
        # anything uncertain 'pending' and block its pair (poll_fills resolves it,
        # the trigger re-fires).
        pend = self.conn.execute(
            "SELECT id, symbol, margin_pair, leverage, txid FROM orders "
            "WHERE status='pending'").fetchall()
        terminal = broker.query_orders([t for (_, _, _, _, t) in pend if t])
        for oid, sym, mpair, lev, txid in pend:
            o = terminal.get(txid)
            status = (o or {}).get("status")
            if o is None or status not in ("closed", "canceled", "expired"):
                log.warning("T/P %s: bid %s not confirmed terminal — leaving pending, "
                            "blocking its pair this pass", sym, txid)
                by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "closes": set(),
                                        "blocked": False})["blocked"] = True
                continue
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            if vol_exec > 0:
                self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?",
                                  (vol_exec, oid))
                by_sym.setdefault(sym, {"mpair": mpair, "lev": lev, "rows": [],
                                        "stops": set(), "closes": set(),
                                        "blocked": False})["rows"].append(oid)
                log.info("T/P %s: bid partially filled %.6g before sweep — closing with the book",
                         sym, vol_exec)
            else:
                self.conn.execute("UPDATE orders SET status='canceled', error='tp-flatten' "
                                  "WHERE id=?", (oid,))
        self.conn.commit()
        # 1b) protective stops: CancelAll should have taken them all; anything still
        # resting gets an individual cancel (fail -> pair stays protected + blocked).
        # Cleared refs are NULLed + committed so a crash/failed close leaves rows
        # that _reprotect_naked_open re-arms.
        for sym, d in by_sym.items():
            for st in d["stops"]:
                if st in oo and broker.cancel_order(st) is None:
                    log.warning("T/P %s: stop %s survived the sweep and cancel FAILED — "
                                "pair stays protected, skipping its close this pass", sym, st)
                    d["blocked"] = True
                    continue
                self.conn.execute("UPDATE orders SET stop_txid=NULL WHERE stop_txid=?", (st,))
            self.conn.commit()
        # 2) fresh definite long volume per pair — no sells rest anymore, so this
        # can only be >= what the closes will find
        kr = broker.open_positions()
        if kr is None:
            log.warning("T/P: OpenPositions unavailable after stop-cancel — rows are "
                        "reprotectable; re-trigger next poll")
            return False, False
        positions = list(kr.values()) if isinstance(kr, dict) else []
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}

        def _long_vol(pos):
            if str(pos.get("type", "")).lower() == "sell":
                return 0.0
            try:
                return max(0.0, float(pos.get("vol", 0) or 0) - float(pos.get("vol_closed", 0) or 0))
            except (TypeError, ValueError):
                return 0.0

        # Shape sanity (verify_open_stops' rule): entries that parse to NO long
        # volume mean an unexpected response shape — retiring rows off it would
        # empty the ledger while real positions float. Bail; rows are already
        # reprotectable and the trigger re-fires.
        if kr and not any(_long_vol(p) > 0 for p in positions):
            log.warning("T/P: OpenPositions returned %d entries but no parseable long "
                        "volume — unexpected shape, aborting closes", len(kr))
            return False, False

        # 2b) close orders are LIMIT + post-only now (operator 2026-07-21: market
        # closes paid taker fees + spread every cycle). Each pass pegs a maker sell
        # one tick above best bid; an unfilled sell whose price the market fell away
        # from is canceled and re-pegged next pass (a slow chase that follows the
        # bid down, never crossing). The pass completes only when every pair's live
        # volume is gone, and tp_flatten_active keeps the caller re-firing until
        # then — so a resting close is never stranded by equity dipping under the
        # target mid-chase. One batched status query + one batched Ticker call.
        close_info = broker.query_orders(
            [t for d in by_sym.values() for t in d["closes"]])
        need_quote = [rest_by_ws.get(s, "") for s, d in by_sym.items()
                      if not d["blocked"] and rest_by_ws.get(s)]
        quotes = rest_client.fetch_ticker(sorted(set(need_quote))) if need_quote else {}
        quotes = quotes or {}

        def _quote(sym):
            """(bid, ask) for sym from the batched Ticker response, or None."""
            q = quotes.get(rest_by_ws.get(sym, ""))
            if q is None:       # response key can differ from the requested id
                key = rest_by_ws.get(sym, "")
                q = next((v for k, v in quotes.items() if _norm_pair_key(k) == key), None)
            try:
                return (float(q["b"][0]), float(q["a"][0])) if q else None
            except (TypeError, ValueError, KeyError, IndexError):
                return None

        complete = True
        for sym, d in by_sym.items():
            if d["blocked"]:
                complete = False
                continue
            key = rest_by_ws.get(sym, "")
            vol = sum(_long_vol(p) for p in positions
                      if _norm_pair_key(p.get("pair", "")) == key)
            info = store.get_pair_info(self.conn, sym) or {}
            lot_dec = info.get("lot_decimals")
            if lot_dec is not None:
                vol = _round_down(vol, lot_dec)
            if vol <= 0:
                # exchange already flat (close filled / stop swept earlier) — retire
                # rows; any close order for this pair is done or self-cancels with
                # zero volume left, but cancel a still-resting one to be tidy.
                for t in d["closes"]:
                    if (close_info.get(t) or {}).get("status") in ("open", "pending"):
                        broker.cancel_order(t)
                # Record realized P&L per lot before retiring. Until 2026-07-27 the
                # flatten wrote the plain marker 'tp-flatten', so the exit path that
                # retires MOST positions left no per-lot record at all — 373 closed rows
                # against 21 priced stop exits, and no way to measure the book's edge.
                pnl_map = self._flatten_exit_pnl_map(sym, d["rows"], d["closes"], close_info)
                for oid in d["rows"]:
                    self.conn.execute("UPDATE orders SET status='closed', close_txid=NULL, "
                                      "error=? WHERE id=?",
                                      (pnl_map.get(oid, "tp-flatten"), oid))
                self.conn.commit()
                log.warning("T/P %s: flat — rows retired (limit close filled or no volume)", sym)
                self._journal("tp", sym, "flatten: pair flat — rows retired")
                continue
            complete = False                       # live volume remains — keep working
            # Existing close order: leave a well-placed one resting; clear refs of
            # terminal ones; chase (cancel) one the market fell away from; never act
            # beside an UNKNOWN status (could double the sell volume).
            resting = None
            unknown = False
            for t in d["closes"]:
                o = close_info.get(t)
                status = (o or {}).get("status")
                if o is None:
                    unknown = True
                    log.warning("T/P %s: close %s status UNKNOWN — leaving pair alone "
                                "this pass (never risk a doubled sell)", sym, t)
                elif status in ("open", "pending"):
                    resting = (t, o)
                else:                              # closed/canceled/expired — spent ref
                    self.conn.execute("UPDATE orders SET close_txid=NULL WHERE close_txid=?", (t,))
                    self.conn.commit()
            if unknown:
                continue
            q = _quote(sym)
            if resting is not None:
                t, o = resting
                if q is None:
                    continue                       # can't judge the peg — leave it resting
                bid, ask = q
                tick_dec = config.MARGIN_TICK_DECIMALS.get(sym, 2)
                tick = 10 ** -tick_dec
                try:
                    opx = float((o.get("descr") or {}).get("price") or 0)
                except (TypeError, ValueError):
                    opx = 0.0
                if opx and opx > ask + tick / 2:
                    # market fell away below our sell — re-peg next pass
                    if broker.cancel_order(t) is None:
                        log.warning("T/P %s: chase cancel of %s FAILED — retry next pass", sym, t)
                        continue
                    self.conn.execute("UPDATE orders SET close_txid=NULL WHERE close_txid=?", (t,))
                    self.conn.commit()
                    log.info("T/P %s: close %s @ %s above ask %s — canceled to re-peg", sym, t, opx, ask)
                continue                           # resting at/near the touch — let it work
            # No close resting — place one: maker sell one tick above best bid
            # (clamped to the ask so a sub-tick spread still rests, never crosses).
            if q is None:
                log.warning("T/P %s: no quote — cannot peg a close this pass "
                            "(reprotect re-arms a stop until the next pass)", sym)
                continue
            bid, ask = q
            tick_dec = config.MARGIN_TICK_DECIMALS.get(sym, 2)
            tick = 10 ** -tick_dec
            px = min(_round_price(bid + tick, tick_dec), ask)
            if px <= bid:
                px = ask                           # degenerate book — join the ask
            pxstr = f"{px:.{tick_dec}f}"
            volstr = f"{vol:.{lot_dec}f}" if lot_dec is not None else f"{vol:.8f}"
            params = {"pair": d["mpair"], "type": "sell", "ordertype": "limit",
                      "price": pxstr, "volume": volstr, "leverage": str(d["lev"]),
                      "oflags": "post"}
            res = broker.private("/0/private/AddOrder", params, idempotent=False)
            if res and res.get("txid"):
                txid = res["txid"][0]
                for oid in d["rows"]:
                    self.conn.execute("UPDATE orders SET close_txid=? WHERE id=?", (txid, oid))
                self.conn.commit()
                log.warning("T/P %s: post-only close resting %s @ %s (%s)", sym, volstr, pxstr, txid)
                self._journal("tp", sym, f"flatten: close resting {volstr} @ {pxstr}")
            else:
                log.error("T/P %s: close placement FAILED — rows stay open with stops "
                          "cleared; reprotect re-arms them, next pass retries", sym)
        return True, complete

    # ── per-rung take-profit harvest (operator 2026-07-29 "build it at 4%") ──

    def _rung_exit_pnl_json(self, sym, oid, entry, close_order):
        """Realized P&L for a harvest close -> JSON string, same {'pnl','exit',
        'closed_ts'} shape as the stop/flatten records so one query reads the whole
        ledger. Proceeds are the close's actual cost minus its fee; basis is the
        row's recorded entry price (post-only maker entries fill AT that price).
        Same documented omissions as _flatten_exit_pnl_map: entry-side fee and
        rollover are not subtracted. None when unpriceable — never raises."""
        try:
            v = float(close_order.get("vol_exec", 0) or 0)
            c = float(close_order.get("cost", 0) or 0)
            f = float(close_order.get("fee", 0) or 0)
            e = float(entry or 0)
            if v <= 0 or c <= 0 or e <= 0:
                return None
            pnl = (c - f) - v * e
            ct = close_order.get("closetm")
            try:
                ts = (datetime.datetime.fromtimestamp(float(ct), datetime.timezone.utc).isoformat()
                      if ct else datetime.datetime.now(datetime.timezone.utc).isoformat())
            except (TypeError, ValueError):
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log.info("PNL %s: order %d realized $%.4f (rung harvest)", sym, oid, pnl)
            return json.dumps({"pnl": round(pnl, 8), "exit": "tp-rung", "closed_ts": ts})
        except Exception:
            log.exception("PNL record failed for harvest of order %d (settling anyway)", oid)
            return None

    def _check_rung_harvest(self):
        """Bank individual rungs at entry*(1+TP_RUNG_PCT) — the per-lot harvester the
        07-28 backtest sized (4% target: realized-net peak, 69% win rate, ~1.5 days
        to bank). Two passes: settle/chase the harvests already working, then start
        new ones. Runs every poll; ZERO API calls when no rung is near target and
        nothing is mid-harvest. The whole-book flatten owns the book when active —
        this never runs beside it. Isolated — never raises into poll_fills."""
        if self.mode != "live" or not getattr(config, "TP_RUNG_ENABLED", False):
            return
        try:
            if str(store.meta_get(self.conn, "tp_flatten_active", "") or "") == "1":
                return
            self._harvest_manage_active()
            self._harvest_start_new()
        except Exception:
            log.exception("rung-harvest pass failed (poll_fills continues)")

    def _harvest_manage_active(self):
        """Settle, chase, or abort every resting harvest sell. Safety rules are the
        flatten's: act only on DEFINITE order state; a cancel leaves close_txid in
        place so the NEXT pass settles the terminal truth (a cancel can race a
        partial fill — the terminal vol_exec decides, never our intent). An abort
        clears close_txid so _reprotect_naked_open re-arms the rung's stop."""
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, entry, close_txid "
            "FROM orders WHERE status='open' AND close_txid IS NOT NULL AND mode=?",
            (self.mode,)).fetchall()
        if not rows:
            return
        # A close_txid shared by SIBLING rows is a flatten's (1:N) — never ours.
        by_close = {}
        for r in rows:
            by_close.setdefault(r[6], []).append(r)
        mine = {t: rs[0] for t, rs in by_close.items() if len(rs) == 1}
        if not mine:
            return
        close_info = broker.query_orders(list(mine))
        need_quote = sorted({_REST_PAIR.get(r[1], "") for t, r in mine.items()
                             if _REST_PAIR.get(r[1])
                             and (close_info.get(t) or {}).get("status") in ("open", "pending")})
        quotes = (rest_client.fetch_ticker(need_quote) or {}) if need_quote else {}
        for txid, (oid, sym, mpair, vol, lev, entry, _) in mine.items():
            o = close_info.get(txid)
            if o is None:
                log.warning("HARVEST %s: close %s status UNKNOWN — leaving alone "
                            "(never risk a doubled sell)", sym, txid)
                continue
            status = o.get("status")
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            volf = float(vol or 0)
            if status in ("closed", "canceled", "expired"):
                if vol_exec >= volf - 1e-8 and vol_exec > 0:
                    pnl_json = self._rung_exit_pnl_json(sym, oid, entry, o)
                    self.conn.execute(
                        "UPDATE orders SET status='closed', close_txid=NULL, "
                        "error=COALESCE(?, 'tp-rung') WHERE id=?", (pnl_json, oid))
                    self.conn.commit()
                    try:
                        banked = json.loads(pnl_json)["pnl"] if pnl_json else 0.0
                    except (TypeError, ValueError, KeyError):
                        banked = 0.0
                    log.warning("HARVEST %s: rung %d BANKED $%+.4f (%.6g @ ~+%.1f%% target)",
                                sym, oid, banked, vol_exec,
                                getattr(config, "TP_RUNG_PCT", 0.04) * 100)
                    self._journal("tp-rung", sym, f"rung {oid} banked ${banked:+.4f} "
                                                  f"({vol_exec:.6g} sold)")
                elif vol_exec > 0:
                    # partial fill then terminal: the sold part is banked on the
                    # exchange; shrink the row to the remainder and let reprotect
                    # re-arm its stop. Per-lot P&L for the sold slice lives in the
                    # journal only (the row must keep ONE terminal pnl record).
                    self.conn.execute(
                        "UPDATE orders SET volume=?, close_txid=NULL WHERE id=?",
                        (volf - vol_exec, oid))
                    self.conn.commit()
                    log.warning("HARVEST %s: rung %d PARTIAL %.6g sold, %.6g remains — "
                                "stop re-arms for the remainder", sym, oid, vol_exec, volf - vol_exec)
                    self._journal("tp-rung", sym, f"rung {oid} partial harvest: {vol_exec:.6g} "
                                                  f"sold, {volf - vol_exec:.6g} re-protected")
                else:
                    self.conn.execute("UPDATE orders SET close_txid=NULL WHERE id=?", (oid,))
                    self.conn.commit()
                    log.info("HARVEST %s: rung %d close %s unfilled — stop re-arms", sym, oid, status)
                continue
            # resting: chase within the floor, abort below it
            q = quotes.get(_REST_PAIR.get(sym, ""))
            if q is None:
                key = _REST_PAIR.get(sym, "")
                q = next((v for k, v in quotes.items() if _norm_pair_key(k) == key), None)
            try:
                bid, ask = (float(q["b"][0]), float(q["a"][0])) if q else (None, None)
            except (TypeError, ValueError, KeyError, IndexError):
                bid = ask = None
            if bid is None:
                continue                          # can't judge — leave it resting
            floor_px = float(entry or 0) * (1 + getattr(config, "TP_RUNG_FLOOR_PCT", 0.02))
            tick = 10 ** -config.MARGIN_TICK_DECIMALS.get(sym, 2)
            try:
                opx = float((o.get("descr") or {}).get("price") or 0)
            except (TypeError, ValueError):
                opx = 0.0
            if bid < floor_px:
                if broker.cancel_order(txid) is None:
                    log.warning("HARVEST %s: abort cancel of %s FAILED — retry next pass", sym, txid)
                    continue
                log.warning("HARVEST %s: rung %d ABORTED — bid %.10g under floor %.10g, "
                            "sell canceled, stop re-arms next pass", sym, oid, bid, floor_px)
                self._journal("tp-rung", sym, f"rung {oid} harvest aborted under floor — re-protecting")
                # close_txid stays; next pass reads the terminal vol_exec and settles
            elif opx and opx > ask + tick / 2:
                if broker.cancel_order(txid) is None:
                    log.warning("HARVEST %s: chase cancel of %s FAILED — retry next pass", sym, txid)
                    continue
                log.info("HARVEST %s: close %s @ %s above ask %s — canceled to re-peg", sym, txid, opx, ask)
                # close_txid stays; terminal settle next pass, re-trigger after that
            # else: resting at/near the touch — let it work

    def _harvest_start_new(self):
        """Open new harvests: best rung per pair whose LOCAL price clears the target,
        capped per pass. Order of operations per rung is the safety argument: quote
        confirms the move first, backing is checked against live exchange volume
        minus what sibling stops already commit (reprotect's rule), the stop is
        canceled BEFORE the sell is placed (they never rest together), and a failed
        placement leaves a stop-less row that reprotect re-arms within a cycle."""
        active = {s for (s,) in self.conn.execute(
            "SELECT DISTINCT symbol FROM orders WHERE status='open' "
            "AND close_txid IS NOT NULL AND mode=?", (self.mode,))}
        tgt_mult = 1 + getattr(config, "TP_RUNG_PCT", 0.04)
        floor_mult = 1 + getattr(config, "TP_RUNG_FLOOR_PCT", 0.02)
        best = {}                                 # sym -> (ratio, row)
        for row in self.conn.execute(
                "SELECT id, symbol, margin_pair, volume, leverage, entry, stop_txid "
                "FROM orders WHERE status='open' AND close_txid IS NULL AND mode=? "
                "AND COALESCE(entry,0) > 0 AND COALESCE(volume,0) > 0", (self.mode,)):
            sym = row[1]
            if sym in active:
                continue
            last = self._last_local_price(sym)
            if not last or last < float(row[5]) * tgt_mult:
                continue
            ratio = last / float(row[5])
            if sym not in best or ratio > best[sym][0]:
                best[sym] = (ratio, row)
        if not best:
            return
        cap = int(getattr(config, "TP_RUNG_MAX_PER_PASS", 4) or 4)
        cands = sorted(best.values(), key=lambda t: -t[0])[:cap]
        kr = broker.open_positions()
        if kr is None:
            log.warning("HARVEST: OpenPositions unavailable — cannot verify backing, retry next poll")
            return
        positions = list(kr.values()) if isinstance(kr, dict) else []

        def _pair_long_vol(sym):
            key = _REST_PAIR.get(sym, "")
            tot = 0.0
            for p in positions:
                if _norm_pair_key(p.get("pair", "")) != key:
                    continue
                if str(p.get("type", "")).lower() == "sell":
                    continue
                try:
                    tot += max(0.0, float(p.get("vol", 0) or 0) - float(p.get("vol_closed", 0) or 0))
                except (TypeError, ValueError):
                    continue
            return tot

        stopped_vol = {}          # sym -> volume committed by RESTING sibling stops
        for s, v in self.conn.execute(
                "SELECT symbol, COALESCE(volume,0) FROM orders "
                "WHERE status='open' AND stop_txid IS NOT NULL AND mode=?", (self.mode,)):
            stopped_vol[s] = stopped_vol.get(s, 0.0) + float(v or 0)
        need = sorted({_REST_PAIR.get(r[1][1], "") for r in cands if _REST_PAIR.get(r[1][1])})
        quotes = (rest_client.fetch_ticker(need) or {}) if need else {}
        for ratio, (oid, sym, mpair, vol, lev, entry, stop_txid) in cands:
            q = quotes.get(_REST_PAIR.get(sym, ""))
            if q is None:
                key = _REST_PAIR.get(sym, "")
                q = next((v for k, v in quotes.items() if _norm_pair_key(k) == key), None)
            try:
                bid, ask = (float(q["b"][0]), float(q["a"][0])) if q else (None, None)
            except (TypeError, ValueError, KeyError, IndexError):
                bid = ask = None
            if bid is None or bid <= float(entry) * floor_mult:
                continue                          # quote gone or move already faded
            volf = float(vol or 0)
            committed = stopped_vol.get(sym, 0.0) - (volf if stop_txid else 0.0)
            if _pair_long_vol(sym) - committed < volf - 1e-8:
                log.warning("HARVEST %s: rung %d not backed by free live volume — "
                            "skipping (reconcile will true up)", sym, oid)
                continue
            if stop_txid:
                if broker.cancel_order(stop_txid) is None:
                    log.warning("HARVEST %s: stop cancel FAILED for rung %d — "
                                "still protected, retry next pass", sym, oid)
                    continue
                self.conn.execute("UPDATE orders SET stop_txid=NULL WHERE id=?", (oid,))
                self.conn.commit()
                stopped_vol[sym] = stopped_vol.get(sym, 0.0) - volf
            # This rung has now PROVED the target (local candle cleared it, the live
            # bid confirms it above the floor, backing verified). Pin its protective
            # stop to breakeven before the sell rests, so an abort re-arms there.
            self._ratchet_stop_prot(oid, sym, entry, bid)
            tick_dec = config.MARGIN_TICK_DECIMALS.get(sym, 2)
            tick = 10 ** -tick_dec
            px = min(_round_price(bid + tick, tick_dec), ask)
            if px <= bid:
                px = ask                          # degenerate book — join the ask
            info = store.get_pair_info(self.conn, sym) or {}
            lot_dec = info.get("lot_decimals")
            volstr = f"{volf:.{lot_dec}f}" if lot_dec is not None else f"{volf:.8f}"
            pxstr = f"{px:.{tick_dec}f}"
            res = broker.private("/0/private/AddOrder",
                                 {"pair": mpair, "type": "sell", "ordertype": "limit",
                                  "price": pxstr, "volume": volstr,
                                  "leverage": str(lev), "oflags": "post"},
                                 idempotent=False)
            if res and res.get("txid"):
                self.conn.execute("UPDATE orders SET close_txid=? WHERE id=?",
                                  (res["txid"][0], oid))
                self.conn.commit()
                log.warning("HARVEST %s: rung %d at +%.1f%% — post-only sell resting "
                            "%s @ %s (%s)", sym, oid, (ratio - 1) * 100, volstr, pxstr,
                            res["txid"][0])
                self._journal("tp-rung", sym, f"rung {oid} harvest: sell resting "
                                              f"{volstr} @ {pxstr} (+{(ratio - 1) * 100:.1f}%)")
            else:
                log.error("HARVEST %s: sell placement FAILED for rung %d — stop dropped, "
                          "reprotect re-arms next cycle", sym, oid)
                self._journal("tp-rung", sym, f"rung {oid} harvest sell FAILED — reprotect re-arms")

    def _ratchet_stop_prot(self, oid, sym, entry, bid):
        """Pin a proved rung's PROTECTIVE stop up to entry*(1+TP_RUNG_RATCHET_PCT)
        (breakeven by default). Called once per rung, at the moment its harvest
        commits — the point at which the lot has demonstrably reached the target.
        Every downstream re-arm path (abort, unfilled cancel, partial remainder)
        reads COALESCE(stop_prot, stop), so this one write covers all of them.

        Writes stop_prot, never `stop`: `stop` is the chain's invalidation level
        that _reladder hands to _place_ladder_rung as the next rung's floor and
        sizing denominator, and moving it to breakeven would read as "ladder floor
        reached" and freeze accumulation on a pair that is working.

        Two invariants: MONOTONIC (never lowers an existing protective stop) and
        BELOW THE MARKET (skipped unless the level sits strictly under the live
        bid, so a ratcheted stop can never rest at/above the market and fire on
        contact). Advisory — a failure here never blocks the harvest."""
        if not getattr(config, "TP_RUNG_RATCHET_ENABLED", False):
            return
        try:
            entryf = float(entry or 0)
            bidf = float(bid or 0)
            if entryf <= 0 or bidf <= 0:
                return
            tick_dec = config.MARGIN_TICK_DECIMALS.get(sym, 2)
            px = _round_price(
                entryf * (1 + getattr(config, "TP_RUNG_RATCHET_PCT", 0.0)), tick_dec)
            if px >= bidf:
                log.info("RATCHET %s: rung %d level %s not below bid %s — stop left at "
                         "its invalidation level", sym, oid, f"{px:g}", f"{bidf:g}")
                return
            r = self.conn.execute(
                "SELECT COALESCE(stop_prot, stop) FROM orders WHERE id=?", (oid,)).fetchone()
            cur = float(r[0]) if r and r[0] is not None else None
            if cur is not None and px <= cur:
                return                            # already at/above — never lower a stop
            self.conn.execute("UPDATE orders SET stop_prot=? WHERE id=?", (px, oid))
            self.conn.commit()
            log.warning("RATCHET %s: rung %d proved +%.1f%% — protective stop %s -> %s "
                        "(an abort now re-arms at breakeven; ladder floor unchanged)",
                        sym, oid, getattr(config, "TP_RUNG_PCT", 0.04) * 100,
                        f"{cur:g}" if cur is not None else "none", f"{px:g}")
            self._journal("stop", sym, f"rung {oid} proved target — protective stop "
                                       f"ratcheted to {px:g} (breakeven)")
        except Exception:
            log.exception("RATCHET %s: rung %d failed (harvest continues)", sym, oid)

    def _place_ladder_rung(self, symbol, margin_pair, leverage, stop, filled_price,
                           score=None, required=None):
        """Continuous laddering (config.LADDER_CONTINUOUS): when a bid fills, drop the
        NEXT post-only rung one LADDER_STEP_PCT below the fill — CONVICTION-sized off
        the entry's score (a 7/7 position ladders 2x rungs; score/required ride down
        the chain via the DB), SAME support stop — so accumulation continues down
        toward the stop without waiting for a candle close or a restart. The conviction
        is FROZEN at the entry score for the whole descent (the weekly/daily signals
        don't move intraday; across a daily close they can, but the ladder does not
        re-score — a high-conviction entry keeps doubling down even if the signal later
        decays). Bounded by a NATURAL FLOOR: a rung at/under the stop is not placed.
        At most one LADDER rung per symbol — best-effort, NOT a hard invariant: the
        has_pending_entry check + insert below run on the poll_fills connection while a
        close-triggered _place_entry (which by design does NOT dedup — see the boot-arm
        note in ingest, close bids stack) can insert its own bid on the dispatch thread,
        so a close bid can coexist with a rung (and, in the microsecond check→insert
        window, a second rung). Benign: each bid carries its own txid + stop and
        reconciles independently; a shared lock would gain almost nothing since the
        entry path is unguarded anyway (operator no-blockers stance). Fully isolated —
        never raises into poll_fills, so the fill/stop just secured is never unwound.
        NULL score -> flat 1.0x min."""
        try:
            if not config.LADDER_CONTINUOUS or self.mode != "live":
                return
            if os.path.exists(config.HALT_FILE):
                log.info("LADDER %s: HALT present — no next rung", symbol)
                return
            ok_acc, why = self._accumulation_allowed()
            if not ok_acc:
                log.info("LADDER %s: accumulation paused (regime gate) — %s", symbol, why)
                return
            ok_ml, ml_why = self._stack_margin_ok()     # rungs grow the book (audit #2)
            if not ok_ml:
                log.info("LADDER %s: paused — %s", symbol, ml_why)
                return
            if not filled_price or filled_price <= 0:
                return
            if store.has_pending_entry(self.conn, symbol, mode=self.mode):
                return                                  # skip if a bid already rests (best-effort; see docstring)
            tick = config.MARGIN_TICK_DECIMALS.get(symbol, 2)
            target = _round_price(filled_price * (1 - config.LADDER_STEP_PCT), tick)
            # Post-only survivability (2026-07-12 offline-gap incident): target is
            # anchored to the FILL, which can be hours stale (offline gap, slow poll).
            # If price has since fallen to/below it, the maker bid crosses the book and
            # Kraken cancels it (reason 'Post only order') — silently killing the
            # accumulation chain. Clamp the rung below the LIVE last (same slip idiom
            # as _place_entry) so it always rests. Ticker unavailable -> keep the
            # fill-anchored target (old behavior); _ensure_ladder_rungs retries later.
            live = self._live_last(symbol)
            if live and live > 0:
                lid = _round_price(live * (1 - config.POST_ONLY_SLIP_PCT), tick)
                if target > lid:
                    log.info("LADDER %s: fill-anchored rung %s at/above live %s — "
                             "clamped to %s so the post-only maker rests",
                             symbol, target, live, lid)
                    target = lid
            if stop and target <= stop * (1 + config.LADDER_STOP_BUFFER):
                log.info("LADDER %s: next rung @ %s would hit stop %s — ladder floor reached, holding",
                         symbol, target, stop)
                return
            # Level dedupe: own each price level ONCE. In a choppy range the daily
            # re-arm + ladder would otherwise re-buy the same band (a fill dips 1%,
            # recovers, re-arms, dips again) and stack many overlapping rungs. Skip
            # the rung if we already hold an OPEN position within half a step of it —
            # descends cleanly toward the stop instead of churn-buying the band. The
            # just-filled anchor is a full step above target, so it never self-blocks.
            if self._owns_level_near(symbol, target, config.LADDER_STEP_PCT * 0.5):
                log.info("LADDER %s: already own a rung within half a step of %s — skip "
                         "(own each level once)", symbol, target)
                return
            equity = self.portfolio_value()
            ok, reason = self.rails_ok(equity)          # honor MAX_OPEN / drawdown / loss caps if on
            if not ok:
                log.info("LADDER %s: rails block next rung — %s", symbol, reason)
                return
            # Rebuild a stand-in card from the persisted entry conviction and size the
            # rung through the exact Tier-1 size(card=) path — so a rung is sized the
            # same as its entry would be. size() reads only .score/.required.
            conv = (types.SimpleNamespace(score=score, required=required)
                    if score is not None and required is not None else None)
            sizing = self.size(symbol, target, stop, leverage, equity, card=conv)
            if sizing is None:
                log.warning("LADDER %s: sizing produced nothing — no rung", symbol)
                return
            cmult = sizing.get("conviction_mult", 1.0)
            smult = max(1.0, float(sizing.get("size_mult", 1.0) or 1.0))
            ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult * smult   # conviction+size scaled (see _place_entry)
            if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
                log.error("LADDER %s REFUSED: rung notional $%.2f exceeds %gx-conviction x %gx-size "
                          "ceiling $%.2f", symbol, sizing["notional"], cmult, smult, ceiling)
                return
            # Respend-rate throttle: rungs grow the book, so they meter the same as
            # seeds (debit only once the rung definitely rests). Signals never ladder.
            ok_b, why_b, debit = self._respend_budget_ok(sizing["notional"])
            if not ok_b:
                log.debug("LADDER %s: %s", symbol, why_b)     # per-pair per-cycle — debug
                return
            vol = sizing["volume"]
            userref = _new_userref()
            params = {"pair": margin_pair, "type": "buy", "ordertype": "limit",
                      "volume": str(vol), "leverage": str(leverage),
                      "price": str(target), "oflags": "post",   # post-only can't fill instantly
                      "userref": str(userref)}
            tmeta = {}
            res = broker.private("/0/private/AddOrder", params, idempotent=False, meta=tmeta)
            row = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "symbol": symbol, "margin_pair": margin_pair, "side": "buy",
                "ordertype": "limit", "mode": self.mode, "entry": target, "stop": stop,
                "volume": vol, "leverage": leverage, "notional": sizing["notional"],
                "margin": sizing["margin"], "risk_usd": sizing.get("actual_risk", 0.0),
                # carry the entry conviction to the next rung so it doesn't decay to 1x
                "score": score, "required": required,
                "txid": None, "stop_txid": None, "status": "pending", "error": None,
                "userref": userref,
            }
            if not (res and res.get("txid")):
                if not tmeta.get("definite"):
                    # Ambiguous transport (audit C3): the rung MAY be on the book — record
                    # it pending with no txid; the userref recovery adopts or retires it.
                    row["error"] = "ambiguous AddOrder (network) — resolving by userref"
                    store.insert_order(self.conn, row)
                    log.warning("LADDER %s: rung AddOrder transport ambiguous — recorded pending, "
                                "userref %s recovery will resolve", symbol, userref)
                    return
                log.warning("LADDER %s: next rung AddOrder returned no txid (post-only reject on a "
                            "gap-down, or transient) — reladder safety net retries in %ds",
                            symbol, _RELADDER_RETRY_SECS)
                return
            row["txid"] = res["txid"][0]
            store.insert_order(self.conn, row)
            debit()                                  # respend budget: the rung rests
            conv_tag = f", {cmult:g}x conviction" if cmult > 1.0 else ""
            log.info("LADDER %s: next rung resting @ %s (%.6g%s) — one step below fill %s",
                     symbol, target, vol, conv_tag, filled_price)
            self._journal("order", symbol, f"ladder: next rung {vol:g} resting @ {target}{conv_tag}")
        except Exception:
            log.exception("LADDER %s: place next rung failed (fill/stop unaffected)", symbol)

    def _adopt_surplus(self, pfx, sym, margin_pair, leftover):
        """Give untracked exchange volume a ledger row so the normal protect path arms
        a stop over it — but ONLY when nothing else could still claim it.

        Untracked volume has two causes that look identical here and must not be
        treated alike:

          * A fill this bot caused but has not booked yet — a resting entry that just
            filled, or an ambiguous AddOrder recorded 'pending' with no txid (see the
            transport branch in _place_next_rung). The userref/fill recovery OWNS that
            volume and will attach it to its real row with the real cost basis.
            Adopting it here would race that recovery and staple a SECOND row, and
            later a second stop, onto one position — the doubled stop-sell that
            _find_adoptable_stop exists to prevent.
          * Volume this bot never created — a manual position, another system. Nothing
            will ever claim it, so alerting alone leaves it naked indefinitely. This is
            the case worth adopting.

        Provenance test: any 'pending' row on this symbol means one of our own fills
        could be in flight -> stay loud, adopt nothing. Otherwise require the surplus to
        persist ADOPT_GRACE_SECS (at least one further reconcile) at a stable volume
        before adopting, so a fill that merely beat its own bookkeeping is never
        mistaken for an external position. Cost basis is the live mark, not the real
        fill price, which we cannot know — open P&L is sourced from Kraken, not from
        this row, so the mark only sets the stop distance.
        """
        if not getattr(config, "ADOPT_UNTRACKED", False):
            return
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE symbol=? AND status='pending'", (sym,)).fetchone()[0]
        if pending:
            self._surplus_seen.pop(sym, None)       # in-flight fill -> restart the clock
            log.warning("%s: %s surplus %.8g NOT adopted — %d pending row(s) on this symbol "
                        "could still claim it (fill recovery owns it)", pfx, sym, leftover, pending)
            return
        now = time.time()
        prev_vol, first_seen = self._surplus_seen.get(sym, (None, None))
        # A moving surplus is still settling; only a stable amount is evidence of a
        # standing external position.
        if prev_vol is None or abs(leftover - prev_vol) > max(1e-8, 0.005 * leftover):
            self._surplus_seen[sym] = (leftover, now)
            log.warning("%s: %s surplus %.8g is new/changed — adoption clock restarted "
                        "(%ds to adopt)", pfx, sym, leftover, config.ADOPT_GRACE_SECS)
            return
        age = now - first_seen
        if age < config.ADOPT_GRACE_SECS:
            log.warning("%s: %s surplus %.8g stable for %ds — adopting in %ds if unclaimed",
                        pfx, sym, leftover, int(age), int(config.ADOPT_GRACE_SECS - age))
            return
        mark = self._live_last(sym)
        if not mark or mark <= 0:
            log.error("%s: %s surplus %.8g ready to adopt but no live mark — retrying next pass",
                      pfx, sym, leftover)
            return
        # Prefer the invalidation level this pair's own lots already sit on, so an
        # adopted lot stops out WITH its siblings rather than at a lone level.
        r = self.conn.execute(
            "SELECT stop FROM orders WHERE symbol=? AND status='open' AND stop IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
        stop = float(r[0]) if r and r[0] else self.compute_stop(sym, mark, None)
        if stop >= mark:
            # Siblings' level is already above the mark (this pair is under water):
            # a stop resting there fires on contact. Fall back to a fresh stop under
            # the mark and say so — late-honoring a level this lot never entered at
            # would be an instant unasked-for market sell.
            log.warning("%s: %s sibling stop %s is at/above mark %s — using a fresh stop "
                        "below the mark for the adopted lot instead", pfx, sym, stop, mark)
            stop = self.compute_stop(sym, mark, None)
            if stop >= mark:
                log.error("%s: %s cannot derive a stop below mark %s — NOT adopting", pfx, sym, mark)
                return
        lev = config.PER_PAIR_LEVERAGE.get(sym)
        oid = store.insert_order(self.conn, {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": sym, "margin_pair": margin_pair, "side": "buy", "ordertype": "adopted",
            "mode": config.EXEC_MODE, "entry": mark, "stop": stop, "volume": leftover,
            "leverage": lev, "notional": mark * leftover,
            "margin": (mark * leftover / lev) if lev else None,
            "status": "open",
            "error": f"adopted untracked exchange volume (external position, "
                     f"unclaimed {int(age)}s); entry is the mark at adoption, not a real fill",
        })
        self._surplus_seen.pop(sym, None)
        log.warning("%s: %s ADOPTED untracked %.8g as row %d @ mark %s, stop %s — protect "
                    "pass will rest its stop", pfx, sym, leftover, oid, mark, stop)
        self._journal("recon", sym, f"adopted untracked {leftover:.8g} — row {oid}, stop {stop}")

    def _find_adoptable_stop(self, kr_open_orders, claimed, margin_pair, want_vol):
        """A protective stop that IS resting on Kraken's book for this pair but whose
        txid no ledger row tracks (a persist-race orphan: AddOrder landed, the DB write
        that records stop_txid then failed). ADOPT it instead of placing a duplicate —
        a doubled stop-sell, when triggered, opens a naked short (rank 3 / gap A).
        Matches a resting sell stop-loss on the same normalized pair, not already
        claimed by another row; prefers the closest volume match. Returns a txid or None
        (None also when OpenOrders was unavailable, i.e. kr_open_orders is falsy)."""
        if not kr_open_orders:
            return None
        target = _norm_pair_key(margin_pair)
        cands = []
        for txid, od in kr_open_orders.items():
            if txid in claimed:
                continue
            d = od.get("descr") or {}
            if str(d.get("type")).lower() != "sell" or "stop" not in str(d.get("ordertype", "")).lower():
                continue
            if _norm_pair_key(d.get("pair", "")) != target:
                continue
            try:
                ovol = float(od.get("vol", 0) or 0)
            except (TypeError, ValueError):
                ovol = 0.0
            cands.append((abs(ovol - want_vol), txid))
        if not cands:
            return None
        cands.sort()
        return cands[0][1]

    def verify_open_stops(self, context="startup"):
        """Reconcile each pair's OPEN ledger rows and their protective stops against
        Kraken's ACTUAL open long volume for that pair. Runs at live restart AND —
        audit 2026-07-13 #1 — on a runtime timer (context='runtime', every
        config.RUNTIME_RECON_SECS from poll_fills), so an intraday stop-fire,
        force-liquidation, or manual close is noticed in minutes, not at the next
        restart. Runtime findings additionally fire safety alerts.
        CRITICAL SAFETY RULE: act ONLY on DEFINITE exchange state. A transient API
        failure (None) is never treated as 'gone'.

        Why PER-PAIR VOLUME, not per-row pair-presence: the strategy stacks MANY
        rows per pair, so "does the pair have *any* position?" is the wrong test —
        a row whose OWN stop already triggered still sees a sibling position, would
        fall through to the re-place branch, and push total resting-stop volume
        ABOVE open volume. If the stops then sweep, the excess sell opens a SHORT on
        the Non-ECP :BTNL book (the exact catastrophe this function exists to
        prevent). Instead we budget each pair's DEFINITE open long volume across its
        open rows oldest-first: a row still backed by remaining volume keeps/gets
        exactly one resting stop; a row with no volume left behind it is closed and
        its stop (if any) canceled as an orphan. Invariant held: resting-stop volume
        per pair <= open volume per pair (never a naked short), while genuinely-open
        volume stays protected. Uncertainty (stop query None) still leaves the row
        untouched for the next restart."""
        if self.mode != "live":
            return
        pfx = context                                      # 'startup' | 'runtime' log tag
        kr = broker.open_positions()
        if kr is None:                                     # could not check -> do NOTHING
            log.warning("%s: OpenPositions unavailable — skipping stop verification", pfx)
            return

        # Kraken OpenPositions is posid -> {"pair": <key|altname>, "vol": str,
        # "vol_closed": str, "type": buy/sell} (hydra field-verified shape). Match on
        # the NORMALIZED pair key: drop any ':SUFFIX', map the four X-prefixed altnames
        # to their canonical key, then EXACT compare (substring matching would let an
        # empty/embedded name mis-match and mis-sum volume). Sum NET open LONG volume;
        # exclude a position only when it is EXPLICITLY typed 'sell' — a missing/odd
        # type still counts, so an unexpected response shape can never silently zero a
        # pair and strip real stops.
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}
        _norm_pair = _norm_pair_key   # module-level; shared with the stop-adopt helper

        def _long_vol(pos):
            if str(pos.get("type", "")).lower() == "sell":
                return 0.0
            try:
                return max(0.0, float(pos.get("vol", 0) or 0) - float(pos.get("vol_closed", 0) or 0))
            except (TypeError, ValueError):
                return 0.0

        positions = list(kr.values()) if isinstance(kr, dict) else []
        # Shape sanity: Kraken returned entries but NONE parse to a long volume -> the
        # response is not the shape we expect. Bail like 'could not check' rather than
        # read every row as unbacked and cancel real protective stops. (An empty dict
        # is the legitimate 'account flat' state and falls through to close rows.)
        if kr and not any(_long_vol(p) > 0 for p in positions):
            log.warning("%s: OpenPositions returned %d entries but no parseable long "
                        "volume — unexpected shape, skipping stop verification", pfx, len(kr))
            return

        def _pair_open_volume(sym):
            key = rest_by_ws.get(sym, "")
            if not key:
                return 0.0
            return sum(_long_vol(p) for p in positions if _norm_pair(p.get("pair", "")) == key)

        # Oldest-first: when part of a pair's stack has closed out, surviving open
        # volume is allocated to the earliest rows; the newest (now-unbacked) rows retire.
        # COALESCE(stop_prot, stop): re-place a proved rung's stop at its RATCHETED
        # protective level (see _ratchet_stop_prot), not the chain invalidation level.
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, COALESCE(stop_prot, stop), "
            "stop_txid, txid, close_txid, "
            "entry FROM orders WHERE status='open' AND mode=? ORDER BY id", (self.mode,)).fetchall()

        # Batch every row's stop status into ONE QueryOrders sweep (50 txids/call)
        # instead of one call per row: a 60-position reconcile otherwise fires 60+
        # back-to-back QueryOrders and trips Kraken's private-API rate limit on every
        # restart. A stop_txid absent from this map reads as None below -> 'status
        # UNKNOWN', identical to the old per-row query_order failure (never re-placed).
        stop_info = broker.query_orders([r[6] for r in rows])   # r[6] = stop_txid
        # Per-rung harvest closes (close_txid held by exactly ONE row — a flatten's is
        # 1:N) must settle BEFORE the volume budget below: a harvest sell that FILLED
        # since the last poll already shrank the pair's live volume, and oldest-first
        # budgeting would otherwise retire the wrong sibling and cancel its LIVE stop
        # as an "orphan" (the 2026-07-20 XLM/ZEC failure shape, harvest edition).
        _close_refs = {}
        for r in rows:
            if r[8]:
                _close_refs.setdefault(r[8], []).append(r[0])
        _solo_closes = [t for t, ids in _close_refs.items() if len(ids) == 1]
        close_order_info = broker.query_orders(_solo_closes) if _solo_closes else {}
        # Kraken's actually-resting orders, so PASS 2 can ADOPT a stop that rests on the
        # book but whose txid the ledger lost (persist-race orphan) instead of placing a
        # duplicate -> naked short (rank 3 / gap A). None on API failure -> adoption is
        # skipped and we fall back to the (already UNKNOWN-guarded) re-place path.
        kr_open_orders = broker.open_orders()
        claimed_stops = {r[6] for r in rows if r[6]}   # stop txids already tracked by a row

        # PASS 1 — classify each row against its pair's DEFINITE open-volume budget and
        # do all REMOVALS now (close unbacked rows, cancel their orphan stops). Only
        # reductions here, so resting-stop volume can never transiently exceed open vol.
        budget = {}
        backed = []
        recon = {}   # per-pair happy-path tally -> a positive evidence line at the end
        for oid, sym, mpair, vol, lev, stop, stop_txid, entry_txid, close_txid, entry in rows:
            key = (sym, mpair)
            if key not in budget:
                budget[key] = _pair_open_volume(sym)
            if sym not in recon:
                recon[sym] = {"rows": 0, "openvol": budget[key], "closed": 0,
                              "resting": 0, "replaced": 0, "unknown": 0}
            recon[sym]["rows"] += 1
            try:
                volf = float(vol or 0)
            except (TypeError, ValueError):
                volf = 0.0
            # Harvest-owned row (solo close_txid): settle it off the CLOSE order's
            # terminal truth first. Filled -> retire w/ P&L, budget preserved for
            # siblings (its volume is already gone from the exchange). Partial/
            # unfilled terminal -> shrink/clear and fall through as an ordinary row
            # (stop re-arms below). Resting -> consume only the UNFILLED remainder.
            # Unknown -> leave everything alone (status quo, consume in full).
            if close_txid and len(_close_refs.get(close_txid, ())) == 1:
                co = close_order_info.get(close_txid)
                cstat = (co or {}).get("status")
                try:
                    cvol = float((co or {}).get("vol_exec", 0) or 0)
                except (TypeError, ValueError):
                    cvol = 0.0
                if cstat in ("closed", "canceled", "expired"):
                    if cvol >= volf - 1e-8 and cvol > 0:
                        pnl_json = self._rung_exit_pnl_json(sym, oid, entry, co)
                        self.conn.execute(
                            "UPDATE orders SET status='closed', close_txid=NULL, "
                            "error=COALESCE(?, 'tp-rung') WHERE id=?", (pnl_json, oid))
                        self.conn.commit()
                        recon[sym]["closed"] += 1
                        log.info("%s: %s order %d harvest close filled — retired w/ P&L, "
                                 "budget preserved for siblings", pfx, sym, oid)
                        self._journal("tp-rung", sym, f"rung {oid} harvest settled by reconcile")
                        continue
                    if cvol > 0:
                        volf -= cvol
                        vol = volf               # backed tuple must carry the remainder
                        self.conn.execute(
                            "UPDATE orders SET volume=?, close_txid=NULL WHERE id=?", (volf, oid))
                    else:
                        self.conn.execute(
                            "UPDATE orders SET close_txid=NULL WHERE id=?", (oid,))
                    self.conn.commit()
                    close_txid = None            # ordinary row now — stop re-arms below
                elif cstat in ("open", "pending"):
                    budget[key] -= max(0.0, volf - cvol)
                    backed.append((oid, sym, mpair, vol, lev, stop, stop_txid, close_txid))
                    continue
                else:
                    # close status UNKNOWN: the sell may rest live — retiring this row
                    # would strand it (an untracked sell). Leave everything alone.
                    recon[sym]["unknown"] += 1
                    log.warning("%s: %s order %d harvest close %s status UNKNOWN — "
                                "leaving as-is, retry next sweep", pfx, sym, oid, close_txid)
                    budget[key] -= volf
                    backed.append((oid, sym, mpair, vol, lev, stop, stop_txid, close_txid))
                    continue
            o = stop_info.get(stop_txid) if stop_txid else None
            ostatus = (o or {}).get("status")
            # (rank 2) This row's OWN stop EXECUTED -> the position is definitively gone,
            # regardless of the pair's remaining open volume (which backs SURVIVING
            # siblings). Close it and record the stop-out P&L, but do NOT consume budget:
            # letting an executed-stop row eat a surviving sibling's backing is exactly
            # what made oldest-first budgeting cancel a live stop and re-place a stale
            # one against the wrong position. Handle this BEFORE the volume budget.
            if ostatus == "closed":
                pnl_json = self._stop_exit_pnl_json(sym, oid, entry_txid, o)
                self.conn.execute("UPDATE orders SET status='closed', error=COALESCE(?, error) WHERE id=?",
                                  (pnl_json, oid))
                self.conn.commit()
                recon[sym]["closed"] += 1
                recon[sym]["stop_fired"] = recon[sym].get("stop_fired", 0) + 1
                log.info("%s: %s order %d stop executed — closed w/ P&L, budget preserved for siblings",
                         pfx, sym, oid)
                self._journal("stop", sym, f"stop EXECUTED — row {oid} closed with P&L recorded")
                continue
            # No open volume left to back this row (and its stop did NOT execute) ->
            # position gone by manual close / liquidation. Its stop, if any, is now an
            # orphan (a stop-sell with no position opens a short). Close the row ONLY once
            # the orphan is PROVABLY neutralized; on cancel failure or UNKNOWN status leave
            # it 'open' and converge next restart — never strand a possibly-live stop by
            # closing its row (the row is then filtered out of every future reconcile).
            if budget[key] < volf - 1e-8:
                if ostatus in ("open", "pending"):
                    if broker.cancel_order(stop_txid) is None:
                        log.warning("%s: %s order %d unbacked but orphan-stop cancel FAILED — "
                                    "leaving open, retry next restart", pfx, sym, oid)
                        recon[sym]["unknown"] += 1
                        continue
                    log.warning("%s: %s order %d unbacked (pair open %.8g < %.8g) — "
                                "canceled orphan stop %s", pfx, sym, oid, budget[key], volf, stop_txid)
                    self._journal("stop", sym, f"canceled orphan stop {stop_txid} (row unbacked)")
                elif o is None and stop_txid:
                    log.warning("%s: %s order %d unbacked but stop status UNKNOWN — "
                                "leaving open, retry next restart", pfx, sym, oid)
                    recon[sym]["unknown"] += 1
                    continue
                # orphan gone (canceled/expired) or no stop_txid: nothing live to strand.
                self.conn.execute("UPDATE orders SET status='closed' WHERE id=?", (oid,))
                self.conn.commit()
                recon[sym]["closed"] += 1
                log.info("%s: %s order %d not backed by open volume — closed, no re-place", pfx, sym, oid)
                continue
            budget[key] -= volf                            # row consumes real open volume
            backed.append((oid, sym, mpair, vol, lev, stop, stop_txid, close_txid))

        # Surplus check (audit 2026-07-13 M1): leftover pair budget after every row is
        # allocated = exchange volume NO ledger row tracks (an ambiguous AddOrder that
        # landed, a manual position, another system). It has NO stop under our control.
        # Silently discarding it was how 'all_ok' overstated coherence. Loud, and it
        # degrades the pair's ok flag; relative threshold so lot-dust never false-alarms.
        for (sym, mp), leftover in budget.items():
            openvol = recon.get(sym, {}).get("openvol", 0.0) or 0.0
            if leftover <= max(1e-8, 0.005 * openvol):
                self._surplus_seen.pop(sym, None)   # cleared -> forget the age history
                continue
            recon[sym]["surplus"] = leftover
            # Severity by provenance, not by symptom. A 'pending' row on this symbol means
            # one of OUR OWN rungs just filled and fill-recovery is about to claim it (it
            # self-heals in ~1s) — routine, and shouting ERROR at it was drowning the real
            # signal this line exists for (07-27 audit: 6/6 of the day's ERRORs were this
            # benign race). Genuinely unclaimable volume — nothing pending, so no stop is
            # coming from us — stays ERROR. Same predicate _adopt_surplus decides on.
            claimable = self.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE symbol=? AND status='pending'", (sym,)).fetchone()[0]
            log.log(logging.WARNING if claimable else logging.ERROR,
                    "%s: %s has %.8g open volume on Kraken NO ledger row tracks — "
                    "untracked position (%s)", pfx, sym, leftover,
                    "fill recovery should claim it" if claimable
                    else "no stop under our control")
            self._journal("recon", sym, f"UNTRACKED exchange volume {leftover:.8g} — no ledger row")
            self._safety("recon-mismatch", sym,
                         f"{leftover:.8g} open volume on Kraken has no ledger row (no stop)")
            self._adopt_surplus(pfx, sym, mp, leftover)
        # ... including pairs with NO ledger rows at all (an exchange position on a pair
        # the loop above never visited — the fully-invisible case) and pairs outside the
        # config universe entirely (ghost pairs — 6 were dropped this week).
        seen_keys = {rest_by_ws.get(s, "") for (s, _m) in budget}
        vol_by_key = {}
        for p in positions:
            k = _norm_pair(p.get("pair", ""))
            if k and k not in seen_keys:
                vol_by_key[k] = vol_by_key.get(k, 0.0) + _long_vol(p)
        ws_by_rest = {p["rest"]: p["ws"] for p in config.PAIRS}
        for k, v in vol_by_key.items():
            if v <= 1e-8:
                continue
            sym = ws_by_rest.get(k, k)              # ghost pairs keep the raw key
            log.error("%s: %s has %.8g open volume on Kraken with ZERO ledger rows — "
                      "fully untracked position", pfx, sym, v)
            self._journal("recon", sym, f"UNTRACKED position {v:.8g} — zero ledger rows")
            self._safety("recon-mismatch", sym,
                         f"{v:.8g} open volume on Kraken with zero ledger rows (no stop)")

        # PASS 2 — ADDITIONS only, after every removal is done: ensure each backed row
        # has exactly one resting stop; re-place only a DEFINITELY-gone/missing one.
        # Fresh-backing gate for re-places (2026-07-30 — the 07-29 ADA race): the
        # backing classification above rode ONE OpenPositions snapshot from sweep
        # start, so a manual close mid-sweep (stops hand-canceled, market close
        # landing) got 4 stops re-placed onto a dying position — orphan sell
        # orders that would have OPENED A SHORT on trigger, resting for a full
        # RUNTIME_RECON_SECS. Before any re-place, re-read OpenPositions ONCE per
        # sweep (lazy — most sweeps re-place nothing) and budget the row against
        # FRESH volume minus what this pass already knows is claimed by sibling
        # rows' resting sells. On doubt (fetch None, or unbacked): do NOT
        # re-place — clear the row's stop_txid so _reprotect_naked_open (every
        # poll, definite-state, backing-gated) owns re-arming within seconds.
        # Protection is deferred one poll, never lost.
        fresh_fetched = False
        fresh_list = None            # None AFTER a fetch == unavailable this sweep
        # Pre-seed each pair's committed volume across ALL rows BEFORE iterating.
        # Accumulating in row order would let a low-id re-place candidate budget
        # against volume a HIGHER-id sibling's live stop already claims — total
        # resting-sell volume above open volume, i.e. the naked short this whole
        # function exists to prevent. Holders = a flatten's resting close, a
        # confirmed-resting stop, or an UNKNOWN-status stop (conservatively
        # assumed to rest). Re-place candidates add themselves below, on success.
        committed_vol = {}
        for _oid, _sym, _mp, _v, _lev, _stop, _stxid, _ctxid in backed:
            _o = stop_info.get(_stxid) if _stxid else None
            _st = (_o or {}).get("status")
            if _ctxid or _st in ("open", "pending") or (_o is None and _stxid):
                committed_vol[_sym] = committed_vol.get(_sym, 0.0) + float(_v or 0)
        for oid, sym, mpair, vol, lev, stop, stop_txid, close_txid in backed:
            volf = float(vol or 0)
            if close_txid:
                # The T/P flatten owns this row — its resting limit close is the
                # exit. Re-placing a stop here would double the sell volume.
                log.info("%s: %s order %d has a resting T/P close (%s) — flatten owns "
                         "it, no stop re-place", pfx, sym, oid, close_txid)
                continue                                   # already in committed_vol
            o = stop_info.get(stop_txid) if stop_txid else None
            status = (o or {}).get("status")
            if status in ("open", "pending"):
                recon[sym]["resting"] += 1
                continue                    # stop confirmed resting (in committed_vol)
            if o is None and stop_txid:
                # Stop status UNKNOWN (query failed) while backed: do NOT re-place
                # blindly (it might already rest -> duplicate -> short). Retry later.
                # Conservatively assumed to rest by the committed_vol pre-pass.
                recon[sym]["unknown"] += 1
                log.warning("%s: %s order %d stop query failed — leaving as-is, retry next restart", pfx, sym, oid)
                continue
            if not config.PROTECTIVE_STOP:                 # stops disabled -> never place one
                continue
            # (rank 3 / gap A) Before placing, adopt a stop that IS resting on Kraken's
            # book for this pair but whose txid the ledger lost (persist-race orphan) —
            # placing a duplicate would double the stop-sell -> naked short when hit.
            adopt = self._find_adoptable_stop(kr_open_orders, claimed_stops, mpair, float(vol or 0))
            if adopt:
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (adopt, oid))
                self.conn.commit()
                claimed_stops.add(adopt)
                recon[sym]["resting"] += 1
                committed_vol[sym] = committed_vol.get(sym, 0.0) + volf
                log.warning("%s: %s order %d adopted resting orphan stop %s (ledger had %s) — "
                            "no duplicate placed", pfx, sym, oid, adopt, stop_txid or "none")
                self._journal("stop", sym, f"adopted resting orphan stop {adopt}")
                continue
            # Stop DEFINITELY gone (closed/canceled/expired) or never placed, no orphan to
            # adopt, and the position is backed: re-place once, non-idempotent transport —
            # but only against FRESH backing (the ADA-race gate declared above PASS 2).
            if not fresh_fetched:
                fresh_fetched = True
                fp = broker.open_positions()
                fresh_list = list(fp.values()) if isinstance(fp, dict) else (
                    None if fp is None else [])
            backed_now = False
            if fresh_list is not None:
                fresh_vol = sum(_long_vol(p) for p in fresh_list
                                if _norm_pair(p.get("pair", "")) == rest_by_ws.get(sym, ""))
                backed_now = fresh_vol - committed_vol.get(sym, 0.0) >= volf - 1e-8
            if not backed_now:
                self.conn.execute("UPDATE orders SET stop_txid=NULL WHERE id=?", (oid,))
                self.conn.commit()
                recon[sym]["unknown"] += 1     # not resting, not re-placed — pair reads not-ok
                log.warning("%s: %s order %d stop gone but FRESH backing %s — NOT re-placing "
                            "(manual close may be landing); stop_txid cleared, reprotect "
                            "re-arms next poll if the position survives", pfx, sym, oid,
                            "unavailable" if fresh_list is None else "insufficient")
                self._journal("stop", sym, f"row {oid}: re-place deferred — fresh backing "
                                           f"{'unavailable' if fresh_list is None else 'insufficient'}, "
                                           f"reprotect owns it")
                continue
            log.warning("%s: %s order %d position backed but stop %s — re-placing",
                        pfx, sym, oid, status or "missing")
            # Stale-price tell (audit M4): if the market gapped BELOW the stored stop
            # while it was off the book, this stop-loss sell triggers the moment it
            # rests — that is the stop honoring itself LATE (correct for a long under
            # its invalidation), but say so loudly instead of letting it read as a
            # surprise market close.
            live = self._live_last(sym)
            if live and stop and live <= float(stop):
                log.warning("%s: %s re-placing stop %s AT/ABOVE live %s — it will trigger "
                            "immediately (late stop honor)", pfx, sym, stop, live)
                self._journal("stop", sym, f"stop {stop} at/above live {live} — will trigger on rest")
            res = broker.private("/0/private/AddOrder", {
                "pair": mpair, "type": "sell", "ordertype": "stop-loss",
                "price": str(stop), "volume": str(vol), "leverage": str(lev), "trigger": "index"},
                idempotent=False)
            if res and res.get("txid"):
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], oid))
                self.conn.commit()
                claimed_stops.add(res["txid"][0])   # a sibling row must not adopt it
                recon[sym]["replaced"] += 1
                committed_vol[sym] = committed_vol.get(sym, 0.0) + volf
                log.info("PROTECT %s: re-placed stop @ %s (%s)", sym, stop, res["txid"][0])
                self._journal("stop", sym, f"re-placed missing stop @ {stop}")
            else:
                log.error("PROTECT %s: re-place FAILED — position may be UNPROTECTED", sym)
                self._journal("stop", sym, "re-place FAILED — position may be UNPROTECTED")
                self._safety("unprotected", sym, "stop re-place FAILED — position may be UNPROTECTED")

        # Positive evidence: one line per pair, so a clean reconcile leaves proof it ran
        # and the invariant held — not silence to interpret (audit re-review). E.g.
        # "reconcile SUI/USD: 3 open rows, 15 open vol on Kraken, 3 stops resting, ...".
        for sym, r in recon.items():
            log.info("reconcile %s: %d open rows, %.6g open vol on Kraken, %d stops resting, "
                     "%d closed, %d re-placed, %d unknown",
                     sym, r["rows"], r["openvol"], r["resting"], r["closed"], r["replaced"], r["unknown"])

        # v6 SURVEY: publish this reconcile as display-truth for the header/BOOK
        # coherence readout. A pair is 'ok' iff no stop status was left UNKNOWN
        # (a query failure that could hide an unprotected row) AND no untracked
        # surplus volume was found (audit M1 — surplus used to pass silently).
        # Since audit 2026-07-13 #1 this runs on the RUNTIME_RECON_SECS timer too,
        # so the stamp is minutes old at most, not boot-old.
        try:
            per_pair = {sym: {"rows": r["rows"], "vol": round(r["openvol"], 8),
                              "stops": r["resting"] + r["replaced"],
                              "ok": r["unknown"] == 0 and not r.get("surplus")}
                        for sym, r in recon.items()}
            payload = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "per_pair": per_pair, "all_ok": all(p["ok"] for p in per_pair.values()),
                       "context": context}
            store.meta_set(self.conn, "last_recon", json.dumps(payload))
        except Exception:
            log.exception("last_recon publish failed (reconcile itself unaffected)")
        total_rows = sum(r["rows"] for r in recon.values())
        total_stops = sum(r["resting"] + r["replaced"] for r in recon.values())
        self._journal("recon", "", f"{len(recon)} pairs · {total_stops}/{total_rows} stops resting")
        # Runtime findings page the operator (audit #1/#3): a stop-fire cluster, an
        # unbacked-row retirement, or a re-placed stop found MID-SESSION is exactly
        # what used to stay invisible until restart.
        if context == "runtime":
            fired = {s: r["stop_fired"] for s, r in recon.items() if r.get("stop_fired")}
            if fired:
                detail = ", ".join(f"{s}×{n}" for s, n in fired.items())
                self._safety("stop-fired", "*", f"protective stops EXECUTED: {detail} — "
                                                f"rows closed with P&L recorded")
            closed_other = sum(r["closed"] - r.get("stop_fired", 0) for r in recon.values())
            if closed_other > 0:
                self._safety("recon-mismatch", "*",
                             f"{closed_other} ledger row(s) retired — position gone without "
                             f"our stop (manual close / liquidation)")
            replaced = sum(r["replaced"] for r in recon.values())
            if replaced > 0:
                self._safety("unprotected", "*",
                             f"{replaced} missing protective stop(s) re-placed mid-session")

    def _stop_exit_pnl_json(self, sym, oid, entry_txid, stop_order):
        """Realized P&L for a STOP-triggered close, from Kraken's own execution records:
        proceeds (stop-sell cost - fee) minus cost basis (entry-buy cost + fee). Returns a
        JSON string {'pnl','exit','closed_ts'} or None. None when the exit isn't a settled
        stop (manual close / liquidation / query failure) — those stay unrecorded and
        realized_pnl_since ignores them; loss caps care about stop-outs, so this is aligned.
        LIMITATION: rollover/financing fees are NOT included, so a held leveraged loss is
        slightly understated (the cap trips marginally late). Best-effort: never raises
        into the close path."""
        try:
            if not (stop_order and stop_order.get("status") == "closed"):
                return None
            s_cost = float(stop_order.get("cost", 0) or 0)
            s_fee = float(stop_order.get("fee", 0) or 0)
            if float(stop_order.get("vol_exec", 0) or 0) <= 0 or s_cost <= 0:
                return None
            eo = broker.query_order(entry_txid) if entry_txid else None
            if not eo:
                return None
            e_cost = float(eo.get("cost", 0) or 0)
            e_fee = float(eo.get("fee", 0) or 0)
            if e_cost <= 0:
                return None
            pnl = (s_cost - s_fee) - (e_cost + e_fee)
            # Bucket by the stop's ACTUAL execution time (Kraken 'closetm'), not restart
            # 'now' — a stop that triggered on a prior day/week must land in THAT window's
            # realized-loss cap, not the restart moment (rank 8; mirrors F6's closed_ts
            # bucketing intent). Fall back to now only when closetm is absent/unparseable.
            ct = stop_order.get("closetm")
            try:
                closed_ts = (datetime.datetime.fromtimestamp(float(ct), datetime.timezone.utc).isoformat()
                             if ct else datetime.datetime.now(datetime.timezone.utc).isoformat())
            except (TypeError, ValueError):
                closed_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log.info("PNL %s: order %d realized $%.4f (stop exit)", sym, oid, pnl)
            return json.dumps({"pnl": round(pnl, 8), "exit": "stop", "closed_ts": closed_ts})
        except Exception:
            log.exception("PNL record failed for order %d (closing anyway)", oid)
            return None

    def _flatten_exit_pnl_map(self, sym, oids, close_txids, close_info):
        """Realized P&L per LOT for a T/P-flatten exit -> {oid: json_str}, same shape the
        stop path records ({'pnl','exit','closed_ts'}) so one query reads the whole
        ledger. Empty dict when the exit can't be priced (no filled close order in
        hand: a stop swept the pair first, a manual close, an unresolved query) —
        those rows retire with the plain 'tp-flatten' marker exactly as before.

        A flatten is 1:N — ONE close order retires every lot on the pair — so proceeds
        are allocated pro-rata by lot volume at the close's actual average fill, with
        its fee spread the same way. Cost basis is the row's own recorded entry price:
        entries rest as post-only MAKER limits, so they fill AT that price and the
        basis is exact.

        Same accounting stance as _stop_exit_pnl_json, and the same two documented
        omissions: the entry-side fee and rollover/financing are not subtracted, so a
        held leveraged loss reads slightly better than it was. Never raises into the
        flatten path — a P&L record must never cost us a close."""
        out = {}
        try:
            vol_x = cost_x = fee_x = 0.0
            closed_ts = None
            for t in close_txids:
                o = close_info.get(t) or {}
                if o.get("status") != "closed":
                    continue
                v = float(o.get("vol_exec", 0) or 0)
                c = float(o.get("cost", 0) or 0)
                if v <= 0 or c <= 0:
                    continue
                vol_x += v
                cost_x += c
                fee_x += float(o.get("fee", 0) or 0)
                closed_ts = closed_ts or o.get("closetm")
            if vol_x <= 0 or cost_x <= 0:
                return {}                       # unpriceable — caller keeps 'tp-flatten'
            exit_px = cost_x / vol_x            # Kraken 'cost' is the quote amount
            fee_per_unit = fee_x / vol_x
            try:
                ts = (datetime.datetime.fromtimestamp(float(closed_ts), datetime.timezone.utc).isoformat()
                      if closed_ts else datetime.datetime.now(datetime.timezone.utc).isoformat())
            except (TypeError, ValueError):
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            qs = ",".join("?" * len(oids))
            rows = self.conn.execute(
                f"SELECT id, volume, entry FROM orders WHERE id IN ({qs})", tuple(oids)).fetchall()
            for oid, vol, entry in rows:
                v = float(vol or 0)
                e = float(entry or 0)
                if v <= 0 or e <= 0:
                    continue                    # can't price this lot — leave it plain
                pnl = v * (exit_px - fee_per_unit) - v * e
                out[oid] = json.dumps({"pnl": round(pnl, 8), "exit": "tp-flatten",
                                       "closed_ts": ts})
            if out:
                log.info("PNL %s: %d lot(s) recorded at flatten exit %.10g (realized $%.4f)",
                         sym, len(out),
                         exit_px, sum(json.loads(j)["pnl"] for j in out.values()))
        except Exception:
            log.exception("PNL %s: flatten P&L record failed (rows retire anyway)", sym)
            return {}
        return out

    def _rest_stop(self, symbol, margin_pair, stop_px, volume, leverage, order_id, paper):
        if not config.PROTECTIVE_STOP:
            return
        if paper:
            self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?",
                              (f"PAPER-STOP-{order_id}", order_id))
            self.conn.commit()
            return
        params = {"pair": margin_pair, "type": "sell", "ordertype": "stop-loss",
                  "price": str(stop_px), "volume": str(volume),
                  "leverage": str(leverage), "trigger": "index"}  # :BTNL rejects 'last'
        res = broker.private("/0/private/AddOrder", params, idempotent=False)
        if res and res.get("txid"):
            self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], order_id))
            self.conn.commit()
            log.info("PROTECT %s: exchange stop @ %s (%s)", symbol, stop_px, res["txid"][0])
            self._journal("stop", symbol, f"protective stop rested @ {stop_px}")
        else:
            log.error("PROTECT %s: FAILED to rest stop — position is UNPROTECTED", symbol)
            self._journal("stop", symbol, "STOP FAILED — position UNPROTECTED")
            self._safety("unprotected", symbol,
                         "stop-rest FAILED — naked leveraged long (reprotect retries each cycle)")
