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
    assert _height(txt) <= 25                        # ~19: compact, no overflow


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
