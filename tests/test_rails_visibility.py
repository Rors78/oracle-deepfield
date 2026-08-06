"""The rails were never invisible for lack of data — only for lack of a reader.

2026-08-05: the live bot ran two consecutive boots with the kill switch down. Every
entry and every ladder rung was refused for the whole of both runs, and the console
showed a healthy book throughout. `rails_ok` had the answer the entire time and it
was already published into `appstate.exec`; deck.html simply never read the key.

Three things came out of that, and each is pinned here:

  1. `rails_detail` — per-rail HEADROOM, so a rail closing is visible days before it
     fires. The kill switch sat 2.6% from firing beforehand.
  2. It must never DISAGREE with `rails_ok`, which alone decides whether real money
     moves. They are separate implementations on purpose (see the docstring), so the
     drift they can develop is pinned by a scenario matrix here.
  3. `_track_rails_block` — a clock on a standing block, persisted in `meta` so it
     survives the restarts, because restarting is exactly what the operator does
     when the bot looks wrong. An in-memory clock would have reset on both boots.
"""
import datetime

import pytest

from deepfield import app, config, store
from deepfield import executor as ex_mod


SYM = "BTC/USD"


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "rails.db"))
    store.upsert_pair(conn, "XXBTZUSD", SYM, "BTC", 0.00005, 0.5, 8)
    return conn


def _exec(conn, mode="live"):
    e = ex_mod.Executor(conn)
    e.mode = mode
    return e


@pytest.fixture(autouse=True)
def _rails_on(monkeypatch, tmp_path):
    """Rails armed, and HALT pointed at a path that does not exist — otherwise a
    stray HALT file in the repo would short-circuit every case into 'halted' and
    the whole module would pass vacuously."""
    monkeypatch.setattr(config, "RAILS_ENABLED", True)
    monkeypatch.setattr(config, "HALT_FILE", str(tmp_path / "no-such-HALT"))


def _rail(detail, name):
    return next(r for r in detail["rails"] if r["name"] == name)


# ── the drift guard ──────────────────────────────────────────────────────────

def test_rails_detail_agrees_with_rails_ok(tmp_path, monkeypatch):
    """THE test of this module. `rails_ok` decides whether money moves and
    `rails_detail` draws the picture; they are deliberately separate code, so the
    one thing that must never happen is a console saying "clear" while the executor
    refuses to buy. Every scenario below is checked both ways."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 3)
    e = _exec(conn)

    scenarios = [
        ("no peak, no book", None, 0.0, 0),
        ("healthy", 1000.0, 1000.0, 0),
        ("kill switch by a hair", 799.0, 1000.0, 0),
        ("kill switch clear by a hair", 801.0, 1000.0, 0),
        ("exactly at the floor", 800.0, 1000.0, 0),
        ("deep drawdown", 100.0, 1000.0, 0),
        ("max open exactly", 1000.0, 1000.0, 3),
        ("max open over", 1000.0, 1000.0, 5),
        ("under max open", 1000.0, 1000.0, 2),
        ("both rails firing", 500.0, 1000.0, 9),
        ("equity unknown", None, 1000.0, 0),
    ]
    for label, equity, peak, n_open in scenarios:
        conn.execute("DELETE FROM orders")
        for i in range(n_open):
            conn.execute("INSERT INTO orders(symbol,status,mode) VALUES(?,'open','live')",
                         (f"P{i}/USD",))
        store.meta_set(conn, "peak_equity", peak)
        conn.commit()

        ok, reason = e.rails_ok(equity)
        detail = e.rails_detail(equity)
        assert detail["ok"] == ok, f"{label}: verdict drifted (detail {detail['ok']} vs {ok})"
        assert detail["reason"] == reason, f"{label}: reason drifted"
        if not ok and detail["rails"]:
            # A blocking verdict must be attributable to a named rail, or the
            # console shows red with four green bars and explains nothing.
            assert any(r["blocking"] for r in detail["rails"]), f"{label}: no rail owns the block"
    conn.close()


def test_detail_declines_to_draw_bars_it_is_not_applying(tmp_path, monkeypatch):
    """HALT and RAILS_ENABLED=False both short-circuit every automatic rail inside
    rails_ok. Reporting per-rail headroom then would draw four limits that are not
    being enforced — reassurance about checks that are switched off."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 1000.0)

    monkeypatch.setattr(config, "RAILS_ENABLED", False)
    d = _exec(conn).rails_detail(1.0)          # would be a catastrophic drawdown
    assert d["ok"] and d["rails"] == [] and d["enabled"] is False

    monkeypatch.setattr(config, "RAILS_ENABLED", True)
    halt = tmp_path / "HALT"
    halt.write_text("x")
    monkeypatch.setattr(config, "HALT_FILE", str(halt))
    d = _exec(conn).rails_detail(1000.0)
    assert d["halt"] is True and not d["ok"] and d["rails"] == []
    conn.close()


# ── headroom: the early warning that was missing ─────────────────────────────

def test_kill_switch_headroom_warns_before_it_fires(tmp_path):
    """The real incident, in miniature. Peak $271.85, equity $223.37 — trading fine,
    but only $5.89 above the floor. rails_ok says a flat 'ok' here; the whole point
    of rails_detail is that this reads as 2.7% headroom instead."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 271.85)
    d = _exec(conn).rails_detail(223.37)
    assert d["ok"], "this equity is genuinely above the floor — rails are clear"

    ks = _rail(d, "kill switch")
    assert not ks["blocking"]
    assert ks["limit"] == pytest.approx(217.48, abs=0.01)      # 80% of peak
    assert ks["headroom"] == pytest.approx(5.89, abs=0.01)
    assert ks["pct"] == pytest.approx(2.71, abs=0.05)
    assert ks["pct"] < config.RAILS_TIGHT_PCT, "must render amber, not green"
    conn.close()


def test_headroom_is_none_when_unmeasurable_not_zero_and_not_full(tmp_path):
    """A missing measurement must never render as a comfortable one. With no peak
    recorded the kill switch cannot be evaluated at all — pct None makes the deck
    print 'not measurable'; 0 would read as FIRING and 100 as perfectly safe, and
    both are lies told with confidence."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 0)
    ks = _rail(_exec(conn).rails_detail(1000.0), "kill switch")
    assert ks["pct"] is None and ks["headroom"] is None and not ks["blocking"]

    # A live equity read that FAILED is a different thing from an unmeasured rail:
    # rails_ok refuses to trade blind, so the kill-switch rail must own that block
    # even though it still has no number to draw. Red with four innocent bars and
    # no rail accountable is worse than silence.
    store.meta_set(conn, "peak_equity", 1000.0)
    d = _exec(conn).rails_detail(None)
    assert not d["ok"], "unknown equity must block — the kill switch cannot be verified"
    ks = _rail(d, "kill switch")
    assert ks["pct"] is None and ks["blocking"] is True
    assert "unreadable" in ks["note"]
    conn.close()


def test_loss_rails_carry_negative_limits(tmp_path):
    """The loss rails are FLOORS, not ceilings: used and limit are both signed, and
    the deck keys its signed formatting off limit<0. If these ever turned positive
    the console would print a $15 loss cap as a $15 gain."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 1000.0)
    d = _exec(conn).rails_detail(1000.0)
    for name, cap in (("daily loss", config.DAILY_LOSS_LIMIT_USD),
                      ("weekly loss", config.WEEKLY_LOSS_LIMIT_USD)):
        r = _rail(d, name)
        assert r["limit"] == -float(cap) < 0
        assert r["pct"] == pytest.approx(100.0), "flat P&L is full headroom"
    conn.close()


def test_every_rail_is_named_and_shaped(tmp_path):
    """The deck iterates whatever it is handed. A rail missing a key renders as
    'undefined' in the operator's face, so the contract is pinned here."""
    conn = _conn(tmp_path)
    store.meta_set(conn, "peak_equity", 1000.0)
    d = _exec(conn).rails_detail(1000.0)
    assert [r["name"] for r in d["rails"]] == [
        "kill switch", "open positions", "daily loss", "weekly loss"]
    for r in d["rails"]:
        assert set(r) == {"name", "used", "limit", "headroom", "pct", "note", "blocking"}
    assert d["tight_pct"] == config.RAILS_TIGHT_PCT, "threshold travels with the data"
    conn.close()


# ── the clock, and the one alert ─────────────────────────────────────────────

def _fired(monkeypatch):
    calls = []
    monkeypatch.setattr(app.alerter, "fire_safety",
                        lambda *a, **k: calls.append((a, k)))
    return calls


def test_block_clock_starts_persists_and_clears(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _fired(monkeypatch)

    assert app._track_rails_block(conn, True, "ok") is None
    since = app._track_rails_block(conn, False, "KILL SWITCH: ...")
    assert since, "a block must stamp its start"

    # Second cycle must NOT re-stamp — that would restart the clock every poll and
    # the inert alert could never come due.
    assert app._track_rails_block(conn, False, "KILL SWITCH: ...") == since

    assert app._track_rails_block(conn, True, "ok") is None
    assert not store.meta_get(conn, "rails_block_since", "")
    conn.close()


def test_clock_survives_a_restart(tmp_path, monkeypatch):
    """The reason the clock lives in `meta` and not in memory. The operator restarts
    the bot when it looks wrong — that is precisely what he did, twice, during the
    incident. An in-memory clock resets on every one of those boots and the 30-minute
    threshold is never reached, so the alert that exists to break the loop is the one
    thing the loop guarantees you never see."""
    conn = _conn(tmp_path)
    _fired(monkeypatch)
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=2)).isoformat()
    store.meta_set(conn, "rails_block_since", old)

    # A fresh process has no memory of the block; the DB does.
    assert app._track_rails_block(conn, False, "KILL SWITCH: ...") == old
    conn.close()


def test_inert_alert_fires_once_after_the_threshold(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)
    monkeypatch.setattr(config, "RAILS_INERT_ALERT_MINS", 30)

    # A young block is ordinary — MAX_OPEN breathes as rungs fill and close.
    store.meta_set(conn, "rails_block_since",
                   (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(minutes=5)).isoformat())
    app._track_rails_block(conn, False, "max open positions (300/300)")
    assert calls == [], "a five-minute block must not page"

    store.meta_set(conn, "rails_block_since",
                   (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(hours=2)).isoformat())
    app._track_rails_block(conn, False, "KILL SWITCH: equity $173.87 < 80% of peak")
    assert len(calls) == 1
    kind, symbol, message = calls[0][0]
    assert kind == "rails-inert"
    assert "NOTHING" in message and "KILL SWITCH" in message
    assert "2.0h" in message, "the operator needs to know how long, not just that"

    # Still blocked on every later cycle — but a standing STATE must not re-page.
    for _ in range(5):
        app._track_rails_block(conn, False, "KILL SWITCH: equity $173.87 < 80% of peak")
    assert len(calls) == 1, "a chronic state re-paging on a timer is how a channel dies"
    conn.close()


def test_clearing_rearms_the_alert(tmp_path, monkeypatch):
    """Fire-once is per EPISODE, not for all time. A second freeze next week is news."""
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)
    monkeypatch.setattr(config, "RAILS_INERT_ALERT_MINS", 30)
    two_h = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=2)).isoformat()

    store.meta_set(conn, "rails_block_since", two_h)
    app._track_rails_block(conn, False, "KILL SWITCH")
    app._track_rails_block(conn, True, "ok")               # recovered
    store.meta_set(conn, "rails_block_since", two_h)       # frozen again, later
    app._track_rails_block(conn, False, "KILL SWITCH")
    assert len(calls) == 2
    conn.close()


def test_tracking_never_raises_into_the_poll_loop(tmp_path, monkeypatch):
    """This runs inside the exec-refresh cycle. A display clock must never be the
    reason the bot stops polling."""
    conn = _conn(tmp_path)
    _fired(monkeypatch)
    monkeypatch.setattr(store, "meta_get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app._track_rails_block(conn, False, "KILL SWITCH") is None
    conn.close()


# ── phantom drawdown: equity falling because money changed pockets ───────────

def test_collateral_shift_names_a_phantom_drawdown(tmp_path, monkeypatch):
    """The incident itself. Kraken reported the whole account at $225.30 and the
    margin-collateral subset at $173.83; equity derives from the latter, so it fell
    $51.47 with no trade, no loss and no ledger flow — and the kill switch, which
    only knows equity vs peak, fired on it."""
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)

    # First reading only ANCHORS. Alerting on it would fire on every fresh DB.
    assert app._check_collateral_shift(conn, {"eb": "225.2995", "tb": "225.2995"}) == 0.0
    assert calls == []

    # Kraken's actual TradeBalance response from the incident.
    gap = app._check_collateral_shift(conn, {"eb": "225.2995", "tb": "173.8278"})
    assert gap == pytest.approx(51.47, abs=0.01)
    assert len(calls) == 1
    kind, _sym, message = calls[0][0]
    assert kind == "collateral-shift"
    assert calls[0][1].get("loud") is False, "a standing gap is a state, not a page"
    assert "left MARGIN COLLATERAL" in message and "no trade and no loss" in message
    assert "51.47" in message
    conn.close()


def test_collateral_shift_reports_but_never_corrects(tmp_path, monkeypatch):
    """Deliberately does NOT shift peak_equity. tb also moves when a non-USD
    collateral holding is REVALUED, and that IS a real drawdown — absorbing the
    delta automatically would disarm the kill switch exactly when it should fire."""
    conn = _conn(tmp_path)
    _fired(monkeypatch)
    store.meta_set(conn, "peak_equity", 271.85)
    app._check_collateral_shift(conn, {"eb": "225.30", "tb": "225.30"})
    app._check_collateral_shift(conn, {"eb": "225.30", "tb": "173.83"})
    assert float(store.meta_get(conn, "peak_equity")) == 271.85
    conn.close()


def test_collateral_noise_and_recovery(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)
    app._check_collateral_shift(conn, {"eb": "225.30", "tb": "173.83"})

    # Sub-dollar drift is rounding and valuation noise, not an event.
    app._check_collateral_shift(conn, {"eb": "225.30", "tb": "173.40"})
    assert calls == [], "a 43-cent wobble must not narrate"

    # Money coming BACK is the other half of the story — this is what actually
    # cleared the freeze on 2026-08-05, and it deserves a line too.
    app._check_collateral_shift(conn, {"eb": "223.4307", "tb": "223.4306"})
    assert len(calls) == 1
    assert "returned to margin collateral" in calls[0][0][2]
    conn.close()


def test_collateral_shift_tolerates_a_useless_balance(tmp_path, monkeypatch):
    """Runs in the poll loop next to the margin watch; a missing or junk field must
    be a no-op, never an exception and never a fabricated event."""
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)
    for bal in (None, {}, {"eb": "225.30"}, {"eb": "x", "tb": "y"}, {"eb": None, "tb": 1}):
        assert app._check_collateral_shift(conn, bal) is None
    assert calls == []
    conn.close()


def test_garbage_timestamp_reanchors_rather_than_alerting(tmp_path, monkeypatch):
    """A corrupt stamp must not be parsed into an arbitrary age and paged off."""
    conn = _conn(tmp_path)
    calls = _fired(monkeypatch)
    store.meta_set(conn, "rails_block_since", "not-a-date")
    out = app._track_rails_block(conn, False, "KILL SWITCH")
    assert out and out != "not-a-date" and calls == []
    conn.close()
