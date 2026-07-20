"""Kraken WebSocket v2 client. SPEC §6.

DISCREPANCY vs spec (posted + adapted, M4): §6 says "one connection carries
everything: 45 subs". Live-verified 2026-07-04: Kraken v2 allows only ONE ohlc
interval per symbol per connection — a second ohlc@interval subscribe on the
same connection ACKs success:false ("Already subscribed to one ohlc interval on
this symbol") for all 15 symbols. Isolated test confirmed ticker+one-ohlc-interval
coexist fine (30/30 ACK ok); it's specifically a second ohlc interval that's
rejected. Fix: TWO connections sharing one queue — conn A: ticker+ohlc@1440 (30
subs), conn B: ohlc@10080 (15 subs). See wsdrill.py for the two-instance wiring.

Each WSClient instance: one connection, an explicit `subs` list of channel specs.
Emits normalized events (Tick/CandleUpdate/CandleClosed/LinkUp/LinkDown) onto a
shared asyncio.Queue. Per-symbol ACK success=false is a loud failure. Keepalive
ping <=30s; watchdog: silence >WATCHDOG_SECS forces reconnect. Reconnect: 3
immediate then backoff 5->60s +/-20% jitter, kept far under Cloudflare's
~150/10min. Every (re)connect resubscribes then triggers a gap-heal.
interval_begin is canonical (unix from the RFC3339 string); `timestamp` is
deprecated.

Candle-close detection (§5): a close is emitted when a NEW interval_begin arrives
on a live UPDATE (not the snapshot, which replays history in ascending order) for
a pair/interval. The clock+grace+REST-confirm fallback for silent low-volume
borders is owned by the writer/reconciler (M5).
"""
import json
import time
import random
import asyncio
import logging
import datetime

import websockets

from . import events

log = logging.getLogger("deepfield.ws")

WS_URL = "wss://ws.kraken.com/v2"
PING_SECS = 20          # <=30s keepalive
WATCHDOG_SECS = 20      # no inbound for this long -> force reconnect
SUSPECT_SECS = 10       # log a warning at this point
IMMEDIATE_RETRIES = 3   # attempts before backoff kicks in
BACKOFF_CAP = 60.0


def parse_interval_begin(s):
    """RFC3339 (nanosecond) -> unix int. '2026-01-01T00:00:00.000000000Z'."""
    if isinstance(s, (int, float)):
        return int(s)
    s = s.rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        s = base + "." + frac[:6]
    dt = datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


class WSClient:
    def __init__(self, symbols, queue, subs, on_connect=None, name="ws"):
        """subs: list of channel specs, e.g. [{"channel":"ticker"},
        {"channel":"ohlc","interval":1440}]. Kraken v2 allows only ONE ohlc
        interval per symbol per connection (live-verified) — §6's '45 subs on
        one connection' is not achievable; run two WSClients (see wsdrill.py /
        app wiring) sharing a queue instead."""
        self.symbols = list(symbols)          # v2 symbols e.g. "BTC/USD"
        self.queue = queue
        self.subs = subs
        self.name = name
        self.on_connect = on_connect          # async callable: gap-heal after (re)subscribe
        self.reconnect_count = 0
        self.last_inbound = 0.0
        self.acks_ok = 0
        self.acks_fail = 0
        self._ws = None
        self._stop = False
        self._force_drop = False
        self._last_ib = {}                    # (symbol, interval) -> last interval_begin

    async def stop(self):
        self._stop = True
        await self._close()

    async def force_drop(self):
        """Simulate a TCP kill (the M4 drill)."""
        self._force_drop = True
        await self._close()

    async def _close(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _backoff(self, attempt):
        if attempt <= IMMEDIATE_RETRIES:
            return 0.0
        base = min(5.0 * (2 ** (attempt - IMMEDIATE_RETRIES - 1)), BACKOFF_CAP)
        jitter = base * 0.2 * (2 * random.random() - 1)
        return max(0.0, base + jitter)

    async def run(self):
        attempt = 0
        while not self._stop:
            attempt += 1
            delay = self._backoff(attempt)
            if delay > 0:
                log.info("reconnect backoff: attempt=%d sleeping %.1fs (Cloudflare-safe)", attempt, delay)
                await asyncio.sleep(delay)
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=None) as ws:
                    self._ws = ws
                    self._force_drop = False
                    self.last_inbound = time.time()
                    log.info("[%s] LINK UP (reconnect #%d): connected %s", self.name, self.reconnect_count, WS_URL)
                    await self.queue.put(events.LinkUp(self.reconnect_count, self.name))
                    await self._subscribe(ws)
                    if self.on_connect is not None:
                        log.info("[%s] gap-heal on (re)connect ...", self.name)
                        try:
                            await self.on_connect(self.symbols)
                        except Exception:
                            log.exception("[%s] gap-heal failed (link stays up, continuing)", self.name)
                    attempt = 0  # healthy connection resets the backoff ladder
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] LINK DOWN: %s", self.name, e)
                await self.queue.put(events.LinkDown(str(e), self.name))
            finally:
                self._ws = None
            if self._stop:
                break
            self.reconnect_count += 1
        log.info("[%s] ws client stopped", self.name)

    async def _subscribe(self, ws):
        # Chunk symbol lists to <=50 per subscribe request: 21 symbols in one
        # message was proven live, but the 132-pair full universe (2026-07-19)
        # rides an unverified payload bound — chunking costs a few extra frames
        # and removes the risk of one oversized request silently part-failing.
        chunks = [self.symbols[i:i + 50] for i in range(0, len(self.symbols), 50)] or [[]]
        reqs = [{"method": "subscribe", "params": dict(spec, symbol=chunk)}
                for spec in self.subs for chunk in chunks]
        for r in reqs:
            await ws.send(json.dumps(r))
        log.info("[%s] sent %d subscribe requests (%s) for %d symbols",
                 self.name, len(reqs),
                 "+".join(s["channel"] + (f"@{s['interval']}" if "interval" in s else "") for s in self.subs),
                 len(self.symbols))

    async def _session(self, ws):
        ping_task = asyncio.ensure_future(self._ping_loop(ws))
        try:
            while not self._stop and not self._force_drop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WATCHDOG_SECS)
                except asyncio.TimeoutError:
                    log.warning("watchdog: no inbound for >%ds -> force reconnect", WATCHDOG_SECS)
                    return
                self.last_inbound = time.time()
                await self._handle(raw)
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except (asyncio.CancelledError, Exception):
                # CancelledError is BaseException (3.8+), not Exception — this is
                # our OWN cancellation of our OWN child task; must not leak up as
                # if run()'s task itself were externally cancelled.
                pass

    async def _ping_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(PING_SECS)
                await ws.send(json.dumps({"method": "ping"}))
                if time.time() - self.last_inbound > SUSPECT_SECS:
                    log.warning("link suspect: %.1fs since last inbound", time.time() - self.last_inbound)
        except (asyncio.CancelledError, Exception):
            return

    async def _handle(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if isinstance(msg, dict) and msg.get("method") == "subscribe":
            self._handle_ack(msg)
            return
        channel = msg.get("channel") if isinstance(msg, dict) else None
        if channel == "heartbeat" or msg.get("method") == "pong":
            return  # liveness only
        if channel == "status":
            log.info("status: %s", msg.get("data"))
            return
        if channel == "ticker":
            for d in msg.get("data", []):
                await self._emit_tick(d)
        elif channel == "ohlc":
            typ = msg.get("type")
            for d in msg.get("data", []):
                await self._emit_candle(d, typ)

    def _handle_ack(self, msg):
        sym = (msg.get("result") or {}).get("symbol", "?")
        ch = (msg.get("result") or {}).get("channel", "?")
        if msg.get("success"):
            self.acks_ok += 1
        else:
            self.acks_fail += 1
            log.error("SUBSCRIBE FAILED %s/%s: %s", ch, sym, msg.get("error"))

    async def _emit_tick(self, d):
        try:
            await self.queue.put(events.Tick(
                symbol=d["symbol"], last=float(d.get("last", 0)), bid=float(d.get("bid", 0)),
                ask=float(d.get("ask", 0)), high24=float(d.get("high", 0)),
                low24=float(d.get("low", 0)), volume24=float(d.get("volume", 0)),
                vwap24=float(d.get("vwap", 0)), change_pct=float(d.get("change_pct", 0)),
                ts=time.time()))
        except Exception:
            log.exception("bad ticker payload: %s", d)

    async def _emit_candle(self, d, typ="update"):
        try:
            sym = d["symbol"]
            interval = int(d["interval"])
            ib = parse_interval_begin(d["interval_begin"])
            key = (sym, interval)
            prev = self._last_ib.get(key)
            # Close-detection only on live UPDATES. The snapshot streams many
            # historical bars in ascending order — those are not fresh closes.
            if typ == "update" and prev is not None and ib > prev:
                await self.queue.put(events.CandleClosed(sym, interval, prev))
            self._last_ib[key] = ib
            await self.queue.put(events.CandleUpdate(
                symbol=sym, interval=interval, interval_begin=ib,
                o=float(d["open"]), h=float(d["high"]), l=float(d["low"]),
                c=float(d["close"]), v=float(d.get("volume", 0))))
        except Exception:
            log.exception("bad ohlc payload: %s", d)
