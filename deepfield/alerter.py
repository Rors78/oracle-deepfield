"""Alert chain — confirmed BUY transitions only. SPEC §11, invariant 7, F10.

Ledger row -> tiered local sound (paplay generated wav -> aplay raw PCM -> bell)
-> notify-send if present -> Telegram iff env vars set. Each sound tier is
guarded by shutil.which AND artifact existence, and the return code is only ONE
of three things checked -- never trusted alone. That combination is what the
legacy termux-media-player bug lacked: it fired the command and read returncode
0 with no backend present and no output ever produced, silently swallowing every
alert. test_alert() exercises the whole chain end-to-end with kind='test'.
"""
import os
import csv
import sys
import math
import struct
import shutil
import logging
import tempfile
import datetime
import subprocess
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

from . import store
from . import config
from .config import TG_TOKEN, TG_CHAT

log = logging.getLogger("deepfield.alert")

# SPEC §8: "operator is America/Denver". Legacy v4.x logged naive LOCAL
# wall-clock timestamps (datetime.now(), no tz) -- localizing explicitly here
# rather than trusting whatever tz a future deployment host happens to have.
LEGACY_TZ = ZoneInfo("America/Denver")
LEGACY_DENOM = 7  # v4.x had no F3/N-A concept; always scored out of ALL_SIGNALS

_WAV_PATH = os.path.join(tempfile.gettempdir(), "deepfield_alert.wav")
_RAW_PATH = os.path.join(tempfile.gettempdir(), "deepfield_alert.raw")
_FREQ = 880.0
_DURATION = 0.4
_SAMPLE_RATE = 8000


def _write_wav(path):
    n = int(_SAMPLE_RATE * _DURATION)
    samples = bytearray()
    for i in range(n):
        val = int(127 * math.sin(2 * math.pi * _FREQ * i / _SAMPLE_RATE) * 256)
        samples += struct.pack("<h", val)
    data = bytes(samples)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, _SAMPLE_RATE, _SAMPLE_RATE * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(data)))
        f.write(data)


def _write_raw_pcm(path):
    n = int(_SAMPLE_RATE * _DURATION)
    data = bytearray()
    for i in range(n):
        data.append(int(127 * math.sin(2 * math.pi * _FREQ * i / _SAMPLE_RATE)) & 0xFF)
    with open(path, "wb") as f:
        f.write(bytes(data))


def _try_tier(binary, artifact_writer, artifact_path, cmd_builder, name):
    """Guard: binary present (shutil.which) AND artifact actually written to
    non-trivial size AND subprocess exits 0. Any single check failing means we
    fall through to the next tier -- we never trust returncode in isolation."""
    if not shutil.which(binary):
        return False
    try:
        artifact_writer(artifact_path)
    except Exception:
        log.exception("%s: failed to generate audio artifact", name)
        return False
    if not (os.path.exists(artifact_path) and os.path.getsize(artifact_path) > 0):
        log.warning("%s: artifact missing/empty after write, skipping tier", name)
        return False
    try:
        r = subprocess.run(cmd_builder(artifact_path), timeout=3, capture_output=True)
    except Exception:
        log.exception("%s: invocation failed", name)
        return False
    return r.returncode == 0


def _bell():
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def play_alert():
    """paplay -> aplay -> bell. Returns the tier name that actually fired."""
    if _try_tier("paplay", _write_wav, _WAV_PATH, lambda p: ["paplay", p], "paplay"):
        return "paplay"
    if _try_tier("aplay", _write_raw_pcm, _RAW_PATH,
                 lambda p: ["aplay", "-r", str(_SAMPLE_RATE), "-f", "S8", "-c", "1", p], "aplay"):
        return "aplay"
    _bell()
    return "bell"


def _notify_send(symbol, message):
    if not shutil.which("notify-send"):
        return False
    try:
        r = subprocess.run(["notify-send", f"DEEPFIELD BUY: {symbol}", message],
                           timeout=3, capture_output=True)
        return r.returncode == 0
    except Exception:
        log.exception("notify-send failed")
        return False


def _telegram(message):
    """None = not configured (not a failure). True/False = attempted, outcome."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": message}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        log.exception("telegram send failed")
        return False


def _format_message(symbol, price, score, denom, signals):
    top = ", ".join(signals[:3]) if signals else "(no signals)"
    return f"{symbol} BUY {score}/{denom} @ {price}  [{top}]"


def fire(conn, symbol, price, score, denom, signals, kind="confirmed"):
    """Ledger row -> sound -> notify-send -> telegram. kind: confirmed|provisional|test."""
    ts_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    store.insert_alert(conn, ts_iso, symbol, price, score, denom, signals, kind)
    message = _format_message(symbol, price, score, denom, signals)
    sound_tier = play_alert()
    notified = _notify_send(symbol, message)
    tg_result = _telegram(message)
    log.info("ALERT fired kind=%s symbol=%s score=%s/%s sound=%s notify=%s telegram=%s",
             kind, symbol, score, denom, sound_tier, notified, tg_result)
    return {"sound": sound_tier, "notify": notified, "telegram": tg_result}


def test_alert(conn):
    """--test-alert: exercise the entire chain end-to-end, kind='test'."""
    return fire(conn, "TEST/USD", 0.0, 0, 0, ["test-alert"], kind="test")


def import_legacy_csv(conn, csv_path):
    """§12 `import-legacy <csv>`: seed the F10 cooldown ledger from a legacy
    v4.x dca_log.csv, so a symbol logged as BUY there recently doesn't get an
    immediate duplicate alert from DEEPFIELD's first confirmed evaluation.

    Maps legacy display symbols ('LTC') to ws_symbol ('LTC/USD') via
    config.PAIRS; unmapped/malformed rows are counted as skipped, not raised.
    Timestamps are localized as America/Denver (naive legacy local time) then
    converted to UTC to match alerter.fire()'s own storage format -- getting
    this wrong would skew the cooldown by the local UTC offset."""
    display_to_ws = {p["display"]: p["ws"] for p in config.PAIRS}
    imported = skipped = 0
    with open(csv_path, "r", newline="") as f:
        for row in csv.DictReader(f):
            display = (row.get("symbol") or "").strip()
            ws = display_to_ws.get(display)
            if ws is None:
                skipped += 1
                continue
            try:
                naive = datetime.datetime.strptime((row.get("date") or "").strip(), "%Y-%m-%d %H:%M")
            except Exception:
                skipped += 1
                continue
            utc_dt = naive.replace(tzinfo=LEGACY_TZ).astimezone(datetime.timezone.utc)
            try:
                price = float(row.get("price") or 0.0)
                score = int(float(row.get("score") or 0))
            except Exception:
                price, score = 0.0, 0
            signals = [s for s in (row.get("signals") or "").split("|") if s]
            store.insert_alert(conn, utc_dt.isoformat(), ws, price, score, LEGACY_DENOM, signals, kind="confirmed")
            imported += 1
    return {"imported": imported, "skipped": skipped}


def export_alerts_csv(conn, csv_path):
    """§12 `export-csv <path>`: dump the alerts ledger to CSV."""
    rows = conn.execute(
        "SELECT ts, symbol, price, score, denom, signals, kind FROM alerts ORDER BY id"
    ).fetchall()
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "symbol", "price", "score", "denom", "signals", "kind"])
        writer.writerows(rows)
    return len(rows)
