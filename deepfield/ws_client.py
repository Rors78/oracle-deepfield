"""Kraken WebSocket v2 client. SPEC §6.

wss://ws.kraken.com/v2 — one connection, 45 subs (15 ticker + 15 ohlc@1440 +
15 ohlc@10080). v2 symbols via NORMALIZE. Per-symbol subscribe ACKs: success=false
is a loud failure. Keepalive ping <=30s; watchdog 10s suspect / 20s force-reconnect.
Reconnect: 3 immediate then backoff 5->60s +/-20% jitter, kept far under Cloudflare's
~150 attempts/10min. Every (re)connect: resubscribe then gap-heal (REST refetch).
interval_begin is canonical; `timestamp` deprecated. Emits Tick/CandleUpdate/
CandleClosed/LinkUp/LinkDown onto the event queue.

TODO(M4): connect, subscribe, parse, watchdog, reconnect + gap-heal, --test-drop.
"""
