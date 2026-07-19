"""Multi-timeframe ingest (2026-07-19): fast bars are DATA, never a trigger.

The operator asked to broaden the scanned timeframes beyond daily/weekly. The data
side is harmless; the trigger side is not, because ingest edge-gates the order path
on the close INSTANT per symbol rather than per interval — a design forced by daily
and weekly closing at the same instant every Thu (that collapse fixed a real
double-order). Feeding 15m into that same gate breaks the money path two ways:

  1. every 15m close re-fires the confirmed-BUY path: ~96 order attempts/day/symbol
     instead of ~2;
  2. worse and silent — the boot seed takes max(close instant) across the interval
     list, so a 15m bar closing minutes ago seeds _last_fired PAST the daily close
     instant and suppresses daily entries entirely after a restart.

So these tests pin the separation itself, not the interval numbers: whatever is in
INTERVALS, only SIGNAL_INTERVALS may reach scoring, firing, or the boot seed.
"""
import pytest

from deepfield import config, events, store
from deepfield.ingest import Ingest
from deepfield.profiles import FULL
from deepfield.state import AppState


@pytest.fixture
def ing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "mtf.db"))
    conn = store.connect(config.DB_PATH)
    yield Ingest(conn, AppState(), profile=FULL)
    conn.close()


def test_signal_intervals_are_a_subset_of_ingested():
    assert set(config.SIGNAL_INTERVALS) <= set(config.INTERVALS)


def test_fast_intervals_are_ingested_but_never_signal():
    fast = set(config.INTERVALS) - set(config.SIGNAL_INTERVALS)
    assert fast, "no fast intervals ingested — the broaden-horizons change is missing"
    for iv in fast:
        assert iv < min(config.SIGNAL_INTERVALS), \
            "a 'fast' interval must be finer than every signal interval"


def test_order_path_stays_on_daily_and_weekly():
    """The strategy is a weekly/daily cycle thesis; only those may fire orders."""
    assert config.SIGNAL_INTERVALS == (1440, 10080)


def test_fast_close_does_not_fire_or_seed(ing, monkeypatch):
    """A 15m close must not touch the fire dedup or the scoring recompute."""
    fired = []
    monkeypatch.setattr(ing, "_recompute_confirmed",
                        lambda sym, fire=False: fired.append((sym, fire)))
    sym = config.PAIRS[0]["ws"]
    fast = sorted(set(config.INTERVALS) - set(config.SIGNAL_INTERVALS))[0]

    store.upsert_candle(ing.conn, sym, fast, 1_000_000, 1, 1, 1, 1, 1, 0)
    ing.conn.commit()
    ing.handle_candle_closed(events.CandleClosed(sym, fast, 1_000_000))

    assert fired == [], "a fast-interval close reached the scoring/order path"
    assert sym not in ing._last_fired, \
        "a fast close seeded the fire dedup — this is what suppresses daily entries"


def test_signal_close_still_fires(ing, monkeypatch):
    """Guard against over-correcting: daily must still fire."""
    fired = []
    monkeypatch.setattr(ing, "_recompute_confirmed",
                        lambda sym, fire=False: fired.append((sym, fire)))
    sym = config.PAIRS[0]["ws"]

    store.upsert_candle(ing.conn, sym, 1440, 1_000_000, 1, 1, 1, 1, 1, 0)
    ing.conn.commit()
    ing.handle_candle_closed(events.CandleClosed(sym, 1440, 1_000_000))

    assert fired and fired[0][1] is True, "daily close must still fire the order path"


def test_watchdog_only_chases_signal_intervals(ing):
    """find_overdue costs a REST confirm per hit; at 15m something is always
    overdue, which would turn a safety net into a constant REST drip."""
    sym = config.PAIRS[0]["ws"]
    for iv in config.INTERVALS:
        store.upsert_candle(ing.conn, sym, iv, 1_000_000, 1, 1, 1, 1, 1, 0)
    ing.conn.commit()
    overdue = ing.find_overdue(now=2_000_000_000)
    assert {o[2] for o in overdue} <= set(config.SIGNAL_INTERVALS)


def test_one_ws_connection_per_interval_with_ticker_on_daily():
    """Kraken v2 rejects a second ohlc interval on the same socket, so topology
    must scale with INTERVALS. Ticker belongs on the quiet daily socket — it feeds
    execution pricing and must not ride the noisiest, most reconnect-prone one."""
    from deepfield import app
    clients = app._make_ws_clients([p["ws"] for p in config.PAIRS], None)
    assert len(clients) == len(config.INTERVALS)

    by_interval = {}
    for c in clients:
        ohlc = [s for s in c.subs if s["channel"] == "ohlc"]
        assert len(ohlc) == 1, "one ohlc interval per connection (Kraken v2 limit)"
        by_interval[ohlc[0]["interval"]] = c
    assert set(by_interval) == set(config.INTERVALS)

    tickers = [c for c in clients if any(s["channel"] == "ticker" for s in c.subs)]
    assert len(tickers) == 1, "exactly one ticker subscription"
    assert by_interval[1440] is tickers[0], "ticker must ride the daily socket"
