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
import types
import logging
import datetime

from . import store
from . import broker
from . import config
from . import engine

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
               "XRPUSD": "XXRPZUSD", "LTCUSD": "XLTCZUSD"}


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


class Executor:
    def __init__(self, conn):
        self.conn = conn
        self.mode = config.EXEC_MODE

    def _journal(self, kind, symbol, text):
        """Isolated journal emit (v6 JOURNAL view). DISPLAY-ONLY narration — a
        failure here must NEVER delay or drop a fill/stop/order, so every emit
        goes through this try/except (same rule as the alerter dispatch fix).
        Never raises into the money path."""
        try:
            store.journal(self.conn, kind, symbol, text)
        except Exception:
            log.exception("journal emit failed (%s %s) — trade path unaffected", kind, symbol)

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
        n = store.committed_position_count(self.conn)
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
            "SELECT entry FROM orders WHERE symbol=? AND status='open' AND entry IS NOT NULL",
            (symbol,)).fetchall()
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
            if volume <= 0:
                return None
            notional = volume * entry
            return {
                "volume": volume, "notional": notional, "margin": notional / leverage,
                "risk_usd": 0.0, "actual_risk": volume * max(0.0, stop_dist),
                "capped": False, "floored_to_min": mult == 1.0, "size_mode": "min",
                "conviction_mult": mult,
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

    def place_entry(self, symbol, entry_price, card):
        if self.mode == "off":
            return None
        try:
            return self._place_entry(symbol, entry_price, card)
        except Exception:
            log.exception("executor.place_entry failed for %s (never kills the writer)", symbol)
            return None

    def _place_entry(self, symbol, entry_price, card):
        if symbol not in config.MARGIN_PAIR:
            log.error("no :BTNL margin pair for %s — cannot execute", symbol)
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
        ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult
        if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
            log.error("EXEC %s REFUSED: order notional $%.2f exceeds %gx-conviction ceiling $%.2f "
                      "— not sending (sanity guard, not a rail; check the pairs row / EXEC_SIZE_MODE)",
                      symbol, sizing["notional"], cmult, ceiling)
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
        }

        if self.mode == "paper":
            row["txid"] = f"PAPER-{int(datetime.datetime.now().timestamp())}"
            row["status"] = "open"
            oid = store.insert_order(self.conn, row)
            self._rest_stop(symbol, margin_pair, stop_px, vol, leverage, oid, paper=True)
            return oid

        if self.mode == "validate":
            params["validate"] = "true"
            res = broker.private("/0/private/AddOrder", params)
            row["status"] = "validated" if res is not None else "rejected"
            row["error"] = None if res is not None else "validate returned None"
            if res is not None:
                row["txid"] = str((res.get("descr") or {}).get("order", "validated"))
            return store.insert_order(self.conn, row)

        # live: place the post-only maker limit and record it PENDING. A resting
        # limit is NOT a position — do not rest a stop yet (a stop with no position
        # would open a short) and do not count it as open. poll_fills() promotes
        # it to 'open' and rests the protective stop only once Kraken confirms fill.
        res = broker.private("/0/private/AddOrder", params, idempotent=False)
        if res and res.get("txid"):
            row["txid"] = res["txid"][0]
            row["status"] = "pending"
            log.info("ENTRY %s: limit resting @ %s (pending fill) %s", symbol, px, row["txid"])
            conv = f" · {cmult:g}x conviction" if cmult > 1.0 else ""
            self._journal("order", symbol, f"{row['volume']:g} entry limit resting @ {px} (pending){conv}")
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
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, txid, ts, entry, score, required "
            "FROM orders WHERE status='pending'").fetchall()
        for oid, sym, mpair, vol, lev, stop, txid, ts, entry, score, required in rows:
            o = broker.query_order(txid) if txid else None
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
        # Runtime safety net: re-protect any 'open' position whose stop-rest failed.
        self._reprotect_naked_open()

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
            naked = self.conn.execute(
                "SELECT id, symbol, margin_pair, volume, leverage, stop FROM orders "
                "WHERE status='open' AND stop_txid IS NULL").fetchall()
            if not naked:
                return
            kr_open_orders = broker.open_orders()
            claimed = {t for (t,) in self.conn.execute(
                "SELECT stop_txid FROM orders WHERE stop_txid IS NOT NULL")}
            for oid, sym, mpair, vol, lev, stop in naked:
                adopt = self._find_adoptable_stop(kr_open_orders, claimed, mpair, float(vol or 0))
                if adopt:
                    self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (adopt, oid))
                    self.conn.commit()
                    claimed.add(adopt)
                    log.warning("REPROTECT %s: adopted resting orphan stop %s for naked open row %d",
                                sym, adopt, oid)
                    self._journal("stop", sym, f"reprotect: adopted resting stop {adopt}")
                    continue
                log.warning("REPROTECT %s: open row %d unprotected (stop-rest failed earlier) — "
                            "resting stop now", sym, oid)
                self._rest_stop(sym, mpair, stop, vol, lev, oid, paper=False)
        except Exception:
            log.exception("reprotect pass failed (poll_fills unaffected)")

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
        One resting rung per symbol. Fully isolated — never raises into poll_fills, so
        the fill/stop just secured is never unwound. NULL score -> flat 1.0x min."""
        try:
            if not config.LADDER_CONTINUOUS or self.mode != "live":
                return
            if os.path.exists(config.HALT_FILE):
                log.info("LADDER %s: HALT present — no next rung", symbol)
                return
            if not filled_price or filled_price <= 0:
                return
            if store.has_pending_entry(self.conn, symbol):
                return                                  # one resting rung per symbol
            tick = config.MARGIN_TICK_DECIMALS.get(symbol, 2)
            target = _round_price(filled_price * (1 - config.LADDER_STEP_PCT), tick)
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
            ceiling = config.EXEC_MAX_ORDER_NOTIONAL_USD * cmult   # conviction-scaled (see _place_entry)
            if config.EXEC_MAX_ORDER_NOTIONAL_USD > 0 and sizing["notional"] > ceiling:
                log.error("LADDER %s REFUSED: rung notional $%.2f exceeds %gx-conviction ceiling $%.2f",
                          symbol, sizing["notional"], cmult, ceiling)
                return
            vol = sizing["volume"]
            params = {"pair": margin_pair, "type": "buy", "ordertype": "limit",
                      "volume": str(vol), "leverage": str(leverage),
                      "price": str(target), "oflags": "post"}   # post-only can't fill instantly
            res = broker.private("/0/private/AddOrder", params, idempotent=False)
            if not (res and res.get("txid")):
                log.warning("LADDER %s: next rung AddOrder returned no txid (post-only reject on a "
                            "gap-down, or transient) — ladder pauses, resumes on next fill/close", symbol)
                return
            row = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "symbol": symbol, "margin_pair": margin_pair, "side": "buy",
                "ordertype": "limit", "mode": self.mode, "entry": target, "stop": stop,
                "volume": vol, "leverage": leverage, "notional": sizing["notional"],
                "margin": sizing["margin"], "risk_usd": sizing.get("actual_risk", 0.0),
                # carry the entry conviction to the next rung so it doesn't decay to 1x
                "score": score, "required": required,
                "txid": res["txid"][0], "stop_txid": None, "status": "pending", "error": None,
            }
            store.insert_order(self.conn, row)
            conv_tag = f", {cmult:g}x conviction" if cmult > 1.0 else ""
            log.info("LADDER %s: next rung resting @ %s (%.6g%s) — one step below fill %s",
                     symbol, target, vol, conv_tag, filled_price)
            self._journal("order", symbol, f"ladder: next rung {vol:g} resting @ {target}{conv_tag}")
        except Exception:
            log.exception("LADDER %s: place next rung failed (fill/stop unaffected)", symbol)

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

    def verify_open_stops(self):
        """On live restart: reconcile each pair's OPEN ledger rows and their
        protective stops against Kraken's ACTUAL open long volume for that pair.
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
        kr = broker.open_positions()
        if kr is None:                                     # could not check -> do NOTHING
            log.warning("startup: OpenPositions unavailable — skipping stop verification")
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
            log.warning("startup: OpenPositions returned %d entries but no parseable long "
                        "volume — unexpected shape, skipping stop verification", len(kr))
            return

        def _pair_open_volume(sym):
            key = rest_by_ws.get(sym, "")
            if not key:
                return 0.0
            return sum(_long_vol(p) for p in positions if _norm_pair(p.get("pair", "")) == key)

        # Oldest-first: when part of a pair's stack has closed out, surviving open
        # volume is allocated to the earliest rows; the newest (now-unbacked) rows retire.
        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, stop_txid, txid "
            "FROM orders WHERE status='open' ORDER BY id").fetchall()

        # Batch every row's stop status into ONE QueryOrders sweep (50 txids/call)
        # instead of one call per row: a 60-position reconcile otherwise fires 60+
        # back-to-back QueryOrders and trips Kraken's private-API rate limit on every
        # restart. A stop_txid absent from this map reads as None below -> 'status
        # UNKNOWN', identical to the old per-row query_order failure (never re-placed).
        stop_info = broker.query_orders([r[6] for r in rows])   # r[6] = stop_txid
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
        for oid, sym, mpair, vol, lev, stop, stop_txid, entry_txid in rows:
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
                log.info("startup: %s order %d stop executed — closed w/ P&L, budget preserved for siblings",
                         sym, oid)
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
                        log.warning("startup: %s order %d unbacked but orphan-stop cancel FAILED — "
                                    "leaving open, retry next restart", sym, oid)
                        recon[sym]["unknown"] += 1
                        continue
                    log.warning("startup: %s order %d unbacked (pair open %.8g < %.8g) — "
                                "canceled orphan stop %s", sym, oid, budget[key], volf, stop_txid)
                    self._journal("stop", sym, f"canceled orphan stop {stop_txid} (row unbacked)")
                elif o is None and stop_txid:
                    log.warning("startup: %s order %d unbacked but stop status UNKNOWN — "
                                "leaving open, retry next restart", sym, oid)
                    recon[sym]["unknown"] += 1
                    continue
                # orphan gone (canceled/expired) or no stop_txid: nothing live to strand.
                self.conn.execute("UPDATE orders SET status='closed' WHERE id=?", (oid,))
                self.conn.commit()
                recon[sym]["closed"] += 1
                log.info("startup: %s order %d not backed by open volume — closed, no re-place", sym, oid)
                continue
            budget[key] -= volf                            # row consumes real open volume
            backed.append((oid, sym, mpair, vol, lev, stop, stop_txid))

        # PASS 2 — ADDITIONS only, after every removal is done: ensure each backed row
        # has exactly one resting stop; re-place only a DEFINITELY-gone/missing one.
        for oid, sym, mpair, vol, lev, stop, stop_txid in backed:
            o = stop_info.get(stop_txid) if stop_txid else None
            status = (o or {}).get("status")
            if status in ("open", "pending"):
                recon[sym]["resting"] += 1
                continue                                   # stop confirmed resting -> fine
            if o is None and stop_txid:
                # Stop status UNKNOWN (query failed) while backed: do NOT re-place
                # blindly (it might already rest -> duplicate -> short). Retry later.
                recon[sym]["unknown"] += 1
                log.warning("startup: %s order %d stop query failed — leaving as-is, retry next restart", sym, oid)
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
                log.warning("startup: %s order %d adopted resting orphan stop %s (ledger had %s) — "
                            "no duplicate placed", sym, oid, adopt, stop_txid or "none")
                self._journal("stop", sym, f"adopted resting orphan stop {adopt}")
                continue
            # Stop DEFINITELY gone (closed/canceled/expired) or never placed, no orphan to
            # adopt, and the position is backed: re-place once, non-idempotent transport.
            log.warning("startup: %s order %d position backed but stop %s — re-placing",
                        sym, oid, status or "missing")
            res = broker.private("/0/private/AddOrder", {
                "pair": mpair, "type": "sell", "ordertype": "stop-loss",
                "price": str(stop), "volume": str(vol), "leverage": str(lev), "trigger": "index"},
                idempotent=False)
            if res and res.get("txid"):
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], oid))
                self.conn.commit()
                claimed_stops.add(res["txid"][0])   # a sibling row must not adopt it
                recon[sym]["replaced"] += 1
                log.info("PROTECT %s: re-placed stop @ %s (%s)", sym, stop, res["txid"][0])
                self._journal("stop", sym, f"re-placed missing stop @ {stop}")
            else:
                log.error("PROTECT %s: re-place FAILED — position may be UNPROTECTED", sym)
                self._journal("stop", sym, "re-place FAILED — position may be UNPROTECTED")

        # Positive evidence: one line per pair, so a clean reconcile leaves proof it ran
        # and the invariant held — not silence to interpret (audit re-review). E.g.
        # "reconcile SUI/USD: 3 open rows, 15 open vol on Kraken, 3 stops resting, ...".
        for sym, r in recon.items():
            log.info("reconcile %s: %d open rows, %.6g open vol on Kraken, %d stops resting, "
                     "%d closed, %d re-placed, %d unknown",
                     sym, r["rows"], r["openvol"], r["resting"], r["closed"], r["replaced"], r["unknown"])

        # v6 SURVEY: publish this reconcile as display-truth for the header/BOOK
        # coherence readout. A pair is 'ok' iff no stop status was left UNKNOWN
        # (a query failure that could hide an unprotected row); all_ok gates the
        # header's teal 'recon ok' vs alarm 'MISMATCH'. Boot-time stamp — this
        # runs only at startup, so the UI labels its age honestly, not as live.
        try:
            per_pair = {sym: {"rows": r["rows"], "vol": round(r["openvol"], 8),
                              "stops": r["resting"] + r["replaced"], "ok": r["unknown"] == 0}
                        for sym, r in recon.items()}
            payload = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "per_pair": per_pair, "all_ok": all(p["ok"] for p in per_pair.values())}
            store.meta_set(self.conn, "last_recon", json.dumps(payload))
        except Exception:
            log.exception("last_recon publish failed (reconcile itself unaffected)")
        total_rows = sum(r["rows"] for r in recon.values())
        total_stops = sum(r["resting"] + r["replaced"] for r in recon.values())
        self._journal("recon", "", f"{len(recon)} pairs · {total_stops}/{total_rows} stops resting")

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
