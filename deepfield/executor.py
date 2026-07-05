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
"""
import os
import math
import logging
import datetime

from . import store
from . import broker
from . import config

log = logging.getLogger("deepfield.exec")


def _round_down(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f) / f


def _round_price(x, decimals):
    return round(x, decimals)


class Executor:
    def __init__(self, conn):
        self.conn = conn
        self.mode = config.EXEC_MODE

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

    def size(self, symbol, entry, stop, leverage, equity):
        """Returns a sizing dict or None.
        EXEC_SIZE_MODE='min' (default): buy the minimum order — tiny, no liquidation
        worry. 'risk': volume = risk_usd/(entry-stop), margin-capped, min-floored."""
        info = store.get_pair_info(self.conn, symbol) or {}
        lot_dec = info.get("lot_decimals")
        ordermin = info.get("ordermin") or 0.0
        costmin = info.get("costmin") or 0.0
        stop_dist = entry - stop
        if entry <= 0:
            return None

        if config.EXEC_SIZE_MODE == "min":
            volume = self._min_volume(ordermin, costmin, entry, lot_dec)
            if volume <= 0:
                return None
            notional = volume * entry
            return {
                "volume": volume, "notional": notional, "margin": notional / leverage,
                "risk_usd": 0.0, "actual_risk": volume * max(0.0, stop_dist),
                "capped": False, "floored_to_min": True, "size_mode": "min",
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
        s = self.size(symbol, entry, stop, leverage, equity)
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
        sizing = self.size(symbol, entry_price, stop, leverage, equity)
        if sizing is None:
            log.warning("EXEC %s: sizing produced nothing (equity=%s)", symbol, equity)
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
        log.info("EXEC %s [%s] %.6g @ %s x%d lev · stop %s · notional $%.2f margin $%.2f risk $%.2f%s",
                 symbol, self.mode, vol, px, leverage, stop_px, sizing["notional"],
                 sizing["margin"], sizing["actual_risk"],
                 " (FLOORED-min)" if sizing["floored_to_min"] else (" (CAPPED)" if sizing["capped"] else ""))

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
            "risk_usd": sizing["actual_risk"], "txid": None, "stop_txid": None,
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
            "SELECT id, symbol, margin_pair, volume, leverage, stop, txid "
            "FROM orders WHERE status='pending'").fetchall()
        for oid, sym, mpair, vol, lev, stop, txid in rows:
            o = broker.query_order(txid) if txid else None
            if o is None:
                continue                        # transient query failure — retry next cycle
            status = o.get("status")
            try:
                vol_exec = float(o.get("vol_exec", 0) or 0)
            except (TypeError, ValueError):
                vol_exec = 0.0
            if status not in ("closed", "canceled", "expired"):
                if vol_exec > 0:            # PARTIAL fill while still resting = a real
                    # position. Cancel the remainder so it can't grow past the stop,
                    # then protect what filled.
                    broker.cancel_order(txid)
                    self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?", (vol_exec, oid))
                    self.conn.commit()
                    log.info("FILL %s: partial %.6g while resting — canceled remainder, resting stop", sym, vol_exec)
                    self._rest_stop(sym, mpair, stop, vol_exec, lev, oid, paper=False)
                continue                        # else unfilled + resting — patient bid, leave it
            if vol_exec > 0:                     # terminal + filled (fully, or partial then done)
                self.conn.execute("UPDATE orders SET status='open', volume=? WHERE id=?", (vol_exec, oid))
                self.conn.commit()
                log.info("FILL %s: %.6g filled — position open, resting stop", sym, vol_exec)
                self._rest_stop(sym, mpair, stop, vol_exec, lev, oid, paper=False)
            else:
                self.conn.execute("UPDATE orders SET status='canceled', error=? WHERE id=?",
                                  (f"entry {status}, unfilled", oid))
                self.conn.commit()
                log.info("ENTRY %s: %s unfilled — no position", sym, status)

    def verify_open_stops(self):
        """On live restart: confirm each open position's protective stop is still
        resting on Kraken; re-place if it's gone. CRITICAL SAFETY RULE: act ONLY on
        DEFINITE exchange state. A transient API failure (None) is never treated as
        'gone' — otherwise a blip would (a) abandon a real unprotected long, or
        (b) re-place a stop that's actually resting -> two stops -> the second opens
        a SHORT on the Non-ECP :BTNL book. On any uncertainty we leave the row
        untouched and retry next restart."""
        if self.mode != "live":
            return
        kr = broker.open_positions()
        if kr is None:                                     # could not check -> do NOTHING
            log.warning("startup: OpenPositions unavailable — skipping stop verification")
            return
        open_ids = set()
        for _pid, pos in kr.items():
            pr = str(pos.get("pair", ""))
            open_ids.add(pr)
            open_ids.add(pr.split(":")[0])
        rest_by_ws = {p["ws"]: p["rest"] for p in config.PAIRS}

        def _has_position(sym, mpair):
            base = (mpair or "").split(":")[0]          # e.g. XBTUSD
            rest = rest_by_ws.get(sym, "")              # e.g. XXBTZUSD
            return any(x and (x in open_ids or any(x in pid or pid in x for pid in open_ids))
                       for x in (base, rest))

        rows = self.conn.execute(
            "SELECT id, symbol, margin_pair, volume, leverage, stop, stop_txid "
            "FROM orders WHERE status='open'").fetchall()
        for oid, sym, mpair, vol, lev, stop, stop_txid in rows:
            has_pos = _has_position(sym, mpair)
            o = broker.query_order(stop_txid) if stop_txid else None
            status = (o or {}).get("status")
            # Position confirmed GONE (kr succeeded and doesn't list it): close the
            # order, never re-place. This is a DEFINITE state (kr is not None here).
            if not has_pos:
                # If the protective stop somehow still rests, CANCEL it — a stop-sell
                # with no position opens a short if triggered. (Kraken usually
                # auto-cancels on manual close, but don't rely on it.)
                if status in ("open", "pending"):
                    broker.cancel_order(stop_txid)
                    log.warning("startup: %s position gone but stop still resting — canceled orphan %s",
                                sym, stop_txid)
                self.conn.execute("UPDATE orders SET status='closed' WHERE id=?", (oid,))
                self.conn.commit()
                log.info("startup: %s not in OpenPositions — order %d closed, no re-place", sym, oid)
                continue
            # Position IS open. Stop confirmed resting -> fine.
            if status in ("open", "pending"):
                continue
            # Stop status UNKNOWN (query failed -> None) while the position is open:
            # do NOT re-place blindly (it might already rest -> duplicate -> short).
            if o is None:
                log.warning("startup: %s position open but stop query failed — leaving as-is, retry next restart", sym)
                continue
            # Position open AND stop is DEFINITELY gone (closed/canceled/expired):
            # re-place, once, non-idempotent transport (no blind resend duplicate).
            log.warning("startup: %s position OPEN but stop %s — re-placing", sym, status)
            res = broker.private("/0/private/AddOrder", {
                "pair": mpair, "type": "sell", "ordertype": "stop-loss",
                "price": str(stop), "volume": str(vol), "leverage": str(lev), "trigger": "index"},
                idempotent=False)
            if res and res.get("txid"):
                self.conn.execute("UPDATE orders SET stop_txid=? WHERE id=?", (res["txid"][0], oid))
                self.conn.commit()
                log.info("PROTECT %s: re-placed stop @ %s (%s)", sym, stop, res["txid"][0])
            else:
                log.error("PROTECT %s: re-place FAILED — position may be UNPROTECTED", sym)

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
        else:
            log.error("PROTECT %s: FAILED to rest stop — position is UNPROTECTED", symbol)
