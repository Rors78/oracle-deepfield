"""v6 SURVEY acceptance — FIELD height law, band/proximity sort, all-idle
compression, multi-size render, recon mismatch. Pure renderer + AppState; no
network, no live process."""
import json
import time

from deepfield import ui
from deepfield.state import AppState


class Card:
    def __init__(self, status="BUY", score=5, denom=7, lo=100.0, hi=200.0):
        self.status = status
        self.score = score
        self.denom = denom
        self.required = 5
        self.low_52w = lo
        self.high_52w = hi
        self.pct_above_low = 10.0
        self.weekly_rsi = 31.0
        self.wrsi_ref = 30.0
        self.daily_rsi = 52.0
        self.results = []
        self.gap = {}


def _tick(last, chg=0.5):
    return type("T", (), {"last": last, "change_pct": chg})()


def _fills(n, entry=150.0, stop=95.0, lev=10):
    return [{"id": i, "ts": "2026-07-05T00:00:00+00:00", "vol": 5.0, "lev": lev,
             "entry": entry + i, "stop": stop} for i in range(n)]


def _by_pair(fills):
    vsum = sum(f["vol"] for f in fills)
    return {"fills": fills, "pendings": [], "vol_sum": vsum,
            "avg_entry": sum(f["vol"] * f["entry"] for f in fills) / vsum if vsum else None,
            "upnl": None, "stop": max(f["stop"] for f in fills) if fills else None}


def _seed(st, sym, nfills, price=150.0, status="BUY"):
    ps = st.pair(sym)
    ps.confirmed = Card(status=status)
    ps.last_tick = _tick(price); ps.last_tick_ts = time.time()
    if nfills:
        st.exec["by_pair"][sym] = _by_pair(_fills(nfills))


def _height(txt):
    return len(txt.rstrip("\n").split("\n"))


# ── #1 height law + windowed ledger ──────────────────────────────────────────

def test_field_height_within_54_with_30fill_expanded():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    _seed(st, "BTC/USD", 1)
    _seed(st, "ETH/USD", 8)
    _seed(st, "SUI/USD", 30)
    st.focus_symbol = "SUI/USD"
    st.expanded_symbol = "SUI/USD"
    txt = ui.export_frame_text(st, width=229, height=54)
    assert _height(txt) <= 54                       # never overflows the screen
    assert "(20 more)" in txt                        # ledger windowed at 10 of 30


# ── height law airtight: all 15 stacked + one expanded, keybar never clips ───

def test_field_height_law_all_active_plus_expanded():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    for i, sym in enumerate(ui.PAIR_LIST):
        _seed(st, sym, 30 if i == 0 else 6)          # every pair active (has fills)
    st.focus_symbol = ui.PAIR_LIST[0]
    st.expanded_symbol = ui.PAIR_LIST[0]              # the 30-fill one expanded
    txt = ui.export_frame_text(st, width=229, height=54)
    assert _height(txt) <= 54                          # accurate reserve -> never overflows
    assert "1 field · 2 book" in txt                   # keybar present (not clipped off)


# ── Fix 1: band scale by tier ────────────────────────────────────────────────

def test_band_bounds_trade_zone_for_active_year_for_watch():
    active = {"has_fills": True, "stop": 100.0, "price": 110.0,
              "fills": [{"entry": 108.0}, {"entry": 112.0}], "pendings": [],
              "card": type("C", (), {"pct_above_low": 11.0})(), "lo": 40.0, "hi": 200.0}
    lo, hi, note = ui._band_bounds(active)
    assert abs(lo - 108.0 * 0.99) < 1e-9         # anchored to cheapest fill/price, NOT the stop
    assert abs(hi - 112.0 * 1.01) < 1e-9
    assert lo > active["stop"]                    # stop sits OFF-scale, below the zone
    assert note and "52w low" in note
    # proximity pulls the stop back on-scale (so the alarm segment can render)
    prox = {"has_fills": True, "stop": 100.0, "price": 102.0, "fills": [{"entry": 108.0}],
            "pendings": [], "card": None, "lo": 40.0, "hi": 200.0}
    plo, _, _ = ui._band_bounds(prox)
    assert plo <= 100.0
    watch = {"has_fills": False, "stop": None, "price": 50.0, "fills": [], "pendings": [],
             "card": None, "lo": 40.0, "hi": 200.0}
    assert ui._band_bounds(watch) == (40.0, 200.0, None)   # year scale, no zoom


# ── Fix A: the band labels the TRUE stop, not the scale anchor ───────────────

def test_active_band_labels_true_stop_not_anchor():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    ps = st.pair("LTC/USD")
    ps.confirmed = Card(status="BUY", lo=41.14, hi=130.0)
    ps.last_tick = _tick(44.8); ps.last_tick_ts = time.time()
    st.exec["by_pair"]["LTC/USD"] = {
        "fills": [{"id": 1, "ts": "x", "vol": 0.1, "lev": 10, "entry": 45.0, "stop": 41.14}],
        "pendings": [], "vol_sum": 0.1, "avg_entry": 45.0, "upnl": None, "stop": 41.14}
    txt = ui.export_frame_text(st, width=229, height=54)
    assert "stop $41.14" in txt                        # TRUE stop is labeled
    assert "◄" in txt                                  # stop marked off-scale (below the zone)
    assert ui._fmt_price(41.14 * 0.98) not in txt      # no scale-anchor price printed


# ── Fix B: stale data feeding a live position sorts to the fault tier ─────────

def test_stale_position_sorts_to_fault_tier():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    old = time.time() - 10_000
    _seed(st, "SUI/USD", 3, status="BUY")              # fresh BUY -> tier 1
    ps = st.pair("LTC/USD")
    ps.confirmed = Card(status="BUY")
    ps.last_tick = _tick(45.0); ps.last_tick_ts = old  # STALE, and has a position
    st.exec["by_pair"]["LTC/USD"] = _by_pair(_fills(3))
    ordered = ui._attention_sorted(st, time.time())
    assert ordered[0][0] == "LTC/USD" and ordered[0][1] == 0   # stale+position outranks fresh BUY
    # a STALE pair WITHOUT a position is not promoted — old data only matters with money on it
    ps2 = st.pair("ADA/USD")
    ps2.confirmed = Card(status="WATCH", score=3)
    ps2.last_tick = _tick(0.19); ps2.last_tick_ts = old
    tiers = {s: t for s, t, _ in ui._attention_sorted(st, time.time())}
    assert tiers["ADA/USD"] != 0


# ── Fix 3: watch is one row ───────────────────────────────────────────────────

def test_watch_pair_renders_one_row():
    st = AppState()
    now = time.time()
    ps = st.pair("ADA/USD")
    ps.confirmed = Card(status="WATCH", score=3)
    ps.last_tick = _tick(0.19); ps.last_tick_ts = now
    for sym, tier, v in ui._attention_sorted(st, now):
        if sym == "ADA/USD":
            strip = ui._field_strip(st, sym, tier, v, 200, False, now)
            assert len(strip) == 1               # watch = ONE row (bare band)
            return
    raise AssertionError("ADA/USD not in sort")


# ── Fix 2 + Fix 4: header live coverage / recon-separate / P&L labels ─────────

def test_header_live_coverage_and_pnl_labels():
    st = AppState()
    st.exec = dict(st.exec)
    st.exec.update({"mode": "live", "equity": 100.0, "positions": [], "pending": [],
                    "stops_total": 17, "stops_covered": 17, "realized_day": 0.0,
                    "last_recon": json.dumps({"ts": "2026-07-06T06:12:00+00:00",
                                              "per_pair": {}, "all_ok": True})})
    line = ui.render_exec_line(st).plain
    assert "stops 17/17" in line and "recon ok" in line      # live coverage + separate stamp
    assert "open $0.00" in line and "day $0.00" in line       # labeled, and
    assert "+$0.00" not in line                               # zero carries no sign
    st.exec["stops_covered"] = 15
    assert "stops 15/17" in ui.render_exec_line(st).plain     # tracks the live book


# ── #2 band math + proximity promotes to the fault tier ──────────────────────

def test_band_edges_and_proximity_top_tier():
    assert ui._band_col(100.0, 100.0, 200.0, 60) == 0        # 52w low  -> col 0
    assert ui._band_col(200.0, 100.0, 200.0, 60) == 59       # 52w high -> last col
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    # a calm BUY (score 5) and a position sitting 2% above its stop (proximity)
    _seed(st, "ETH/USD", 3, price=150.0, status="BUY")       # calm
    prox = st.pair("SUI/USD")
    prox.confirmed = Card(status="BUY", score=4)
    prox.last_tick = _tick(96.9); prox.last_tick_ts = time.time()   # stop 95 * 1.02
    st.exec["by_pair"]["SUI/USD"] = _by_pair(_fills(3, entry=120.0, stop=95.0))
    ordered = ui._attention_sorted(st, time.time())
    assert ordered[0][0] == "SUI/USD"               # proximity fault sorts to the very top
    assert ordered[0][1] == 0                        # tier 0
    txt = ui.export_frame_text(st, width=229, height=54)
    assert "NEAR-STOP" in txt


# ── #3 all-idle compression ──────────────────────────────────────────────────

def test_all_idle_one_row_each():
    st = AppState()
    txt = ui.export_frame_text(st, width=229, height=54)
    lines = txt.split("\n")
    for sym in ui.PAIR_LIST:
        disp = ui.DISPLAY.get(sym, sym)
        hits = [ln for ln in lines if ln.strip().startswith(disp)]
        assert len(hits) == 1, f"{disp} should render exactly one idle row"
    assert "1 field · 2 book" in txt                 # keybar pinned to the fixed floor
    assert _height(txt) <= 54                          # fills to the floor, no overflow


# ── #4 renders at both sizes, all three views, no crash ──────────────────────

def test_render_all_views_two_sizes_no_crash():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    _seed(st, "BTC/USD", 5)
    st.exec["journal_tail"] = [("2026-07-05T12:00:00+00:00", "fill", "BTC/USD", "5 filled")]
    for w, h in ((229, 54), (160, 45)):
        for view in (1, 2, 3, 1):
            st.view = view
            txt = ui.export_frame_text(st, width=w, height=h)
            assert _height(txt) <= h                  # fits vertically
            assert max((len(l) for l in txt.split("\n")), default=0) <= w   # no h-overflow


# ── BOOK ,/. scroll actually windows the body (not just a state int) ─────────

def test_book_renders_title_and_all_pairs():
    """Guards the header-shadow bug the live run surfaced: the BOOK title must
    render, and with ample height BOTH pairs' headers + a fill from each appear
    (nothing windowed away)."""
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    _seed(st, "LTC/USD", 7, price=46.0)
    _seed(st, "SUI/USD", 8, price=0.74)
    st.view = 2
    txt = ui.export_frame_text(st, width=200, height=54)
    assert txt.lstrip().startswith("BOOK")             # title present (was clobbered by shadow)
    assert "LTC" in txt and "SUI" in txt               # both pair headers
    assert "7f" in txt and "8f" in txt                 # both fill counts
    assert txt.count("live") >= 15                      # 7 + 8 fill rows all rendered
    assert "more)" not in txt                           # everything fits, nothing windowed


def test_book_scroll_windows_body():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    _seed(st, "SUI/USD", 30)
    st.view = 2
    st.book_scroll = 0
    top = ui.export_frame_text(st, width=160, height=20)
    assert top.lstrip().startswith("BOOK")             # title still first even when windowed
    assert "more)" in top                               # windowed → overflow marker
    assert "(↑" not in top                              # nothing scrolled off the top yet
    st.book_scroll = 12
    scrolled = ui.export_frame_text(st, width=160, height=20)
    assert "(↑ 12 above)" in scrolled                   # render consumed book_scroll
    assert scrolled != top


# ── #6 recon mismatch: header MISMATCH + BOOK ▢ on the offending pair ─────────

def test_recon_mismatch_header_and_book():
    st = AppState()
    st.exec = dict(st.exec); st.exec["by_pair"] = {}
    _seed(st, "SUI/USD", 3)
    recon = {"ts": "2026-07-05T23:34:00+00:00",
             "per_pair": {"SUI/USD": {"rows": 3, "vol": 15, "stops": 1, "ok": False}},
             "all_ok": False}
    st.exec.update({"mode": "live", "equity": 100.0, "positions": [],
                    "pending": [], "last_recon": json.dumps(recon)})
    header = ui.render_exec_line(st).plain
    assert "MISMATCH" in header
    st.view = 2
    book = ui.export_frame_text(st, width=229, height=54)
    assert "MISMATCH" in book and "SUI" in book
