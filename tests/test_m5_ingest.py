"""M5 regression tests: ingest recompute/throttle, F10 cooldown, alerter guards."""
import time

from deepfield import store, events, alerter
from deepfield.ingest import Ingest
from deepfield.state import AppState
from deepfield.profiles import FULL

SYM = "TEST/USD"


def _seed_minimal(conn, now):
    """A tiny closed history so store.load_weekly_daily_closed returns non-empty
    series; engine signals degrade to NA/NOT gracefully on short data."""
    for i in range(25):
        ts = now - (25 - i) * 7 * 86400
        store.upsert_candle(conn, SYM, 10080, ts, 10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    for i in range(25):
        ts = now - (25 - i) * 86400
        store.upsert_candle(conn, SYM, 1440, ts, 10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    conn.commit()


def test_provisional_throttle_does_not_consume_window_on_cold_start(tmp_path):
    """Regression: a recompute attempt missing one forming bar must NOT mark the
    throttle window as spent — otherwise the update that WOULD complete the pair
    (arriving milliseconds later) gets throttled away and provisional never runs."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)

    # Weekly-only forming update: daily forming bar doesn't exist yet -> miss.
    ing.handle_candle_update(events.CandleUpdate(SYM, 10080, now, 10.0, 11.0, 9.0, 10.5, 60.0))
    assert ing.state.pair(SYM).provisional is None

    # Daily forming arrives moments later (same wall-clock second) -> must NOT
    # be throttled by the miss above.
    ing.handle_candle_update(events.CandleUpdate(SYM, 1440, now, 10.0, 10.6, 9.8, 10.4, 55.0))
    assert ing.state.pair(SYM).provisional is not None
    conn.close()


def test_confirmed_recompute_alerts_then_cooldown_suppresses_repeat(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ing = Ingest(conn, AppState(), profile=FULL)

    # Force a BUY by monkeypatching evaluate is unnecessary here — we only need
    # to prove the cooldown *mechanism*, so drive it directly via _maybe_alert
    # with a synthetic ScoreCard-like object.
    class Card:
        status = "BUY"; price = 1.0; score = 5; denom = 7; fired = ["x"]

    ing._maybe_alert(SYM, Card(), kind="confirmed")
    n1 = conn.execute("SELECT COUNT(*) FROM alerts WHERE symbol=? AND kind='confirmed'", (SYM,)).fetchone()[0]
    assert n1 == 1

    ing._maybe_alert(SYM, Card(), kind="confirmed")  # immediate repeat
    n2 = conn.execute("SELECT COUNT(*) FROM alerts WHERE symbol=? AND kind='confirmed'", (SYM,)).fetchone()[0]
    assert n2 == 1, "cooldown failed to suppress an immediate repeat alert"
    conn.close()


def test_candle_closed_duplicate_is_not_logged_as_a_gap(tmp_path, caplog):
    """A repeat CandleClosed for an already-closed bar is a harmless duplicate,
    not a data-loss gap — must not emit the gap warning."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    ts = now - 7 * 86400
    store.upsert_candle(conn, SYM, 10080, ts, 10.0, 11.0, 9.0, 10.0, 50.0, closed=1)
    conn.commit()
    ing = Ingest(conn, AppState(), profile=FULL)

    import logging
    with caplog.at_level(logging.WARNING, logger="deepfield.ingest"):
        ing.handle_candle_closed(events.CandleClosed(SYM, 10080, ts))
    assert not any("not in DB yet" in r.message for r in caplog.records)
    conn.close()


def test_alerter_falls_back_to_bell_when_no_audio_binaries(monkeypatch):
    monkeypatch.setattr(alerter.shutil, "which", lambda _name: None)
    tier = alerter.play_alert()
    assert tier == "bell"


def test_alerter_telegram_not_configured_returns_none(monkeypatch):
    monkeypatch.setattr(alerter, "TG_TOKEN", None)
    monkeypatch.setattr(alerter, "TG_CHAT", None)
    assert alerter._telegram("hello") is None


def test_alerter_telegram_configured_posts_to_correct_url(monkeypatch):
    """Can't get real delivery confirmation without operator-supplied bot
    credentials -- this proves the mechanism (correct URL, correct method,
    chat_id/text in the POST body) rather than actual Telegram delivery."""
    monkeypatch.setattr(alerter, "TG_TOKEN", "FAKETOKEN")
    monkeypatch.setattr(alerter, "TG_CHAT", "12345")

    captured = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(alerter.urllib.request, "urlopen", fake_urlopen)
    result = alerter._telegram("BTC/USD BUY 5/7 @ 60000")

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botFAKETOKEN/sendMessage"
    assert captured["method"] == "POST"
    assert b"chat_id=12345" in captured["data"]


def test_tranche_repriced_on_tick_not_stale_from_close(tmp_path):
    """M6 regression: the champion card's 'Live entry' and 'Tranche' price must
    agree — found via the M6 export proof showing two different prices in the
    same card because tranche only recomputed on candle-close."""
    conn = store.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    _seed_minimal(conn, now)
    conn.execute(
        "INSERT INTO pairs(rest_pair, ws_symbol, display, ordermin, costmin, lot_decimals, updated_ts) "
        "VALUES('TESTUSD', ?, 'TEST', 0.1, 0.5, 8, ?)", (SYM, now),
    )
    conn.commit()
    ing = Ingest(conn, AppState(), profile=FULL)

    ing._recompute_confirmed(SYM)  # tranche priced off the close (no tick yet)
    ing.handle_tick(events.Tick(SYM, last=999.0, bid=998, ask=1000, high24=1000,
                                 low24=900, volume24=10, vwap24=950, change_pct=1.0, ts=time.time()))
    assert ing.state.pair(SYM).tranche.price == 999.0
    assert ing.state.pair(SYM).tranche.price_is_live is True
    conn.close()
