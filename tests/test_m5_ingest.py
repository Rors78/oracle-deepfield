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
