# ORACLE DEEPFIELD — Windows desktop / autostart launcher.
#
# Windows port of scripts/deepfield-desktop.sh. Same one-click intent: start the
# bot, serve the web console, open the browser at it. Three things could not port
# and are replaced rather than dropped:
#
#   pgrep    -> the single-instance guard tests the web port. The bot serves the
#               console in-process, so "port answers" IS "a bot is running". No
#               process-name matching, which on Windows would also match a plain
#               `python` running in another venv.
#   tmux     -> no session multiplexer. The bot runs under pythonw (no console
#               window) and the console at :8787 is the attach surface.
#   xdg-open -> Start-Process on the URL uses the default browser.
#
# EXEC MODE: paper by default. This machine is a migration target, not a trading
# host, and the Kraken key is in use elsewhere — the rate limit is per-ACCOUNT,
# not per key. Paper makes no private API call at all: broker.private() returns
# before building a request when no key file exists. Override for a single run
# with -Mode; do not edit the default.

param(
    [ValidateSet("off", "paper", "validate", "live")]
    [string]$Mode = "paper",
    [switch]$NoBrowser,
    [switch]$Windowed          # show the console window (default: hidden)
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Port = if ($env:DEEPFIELD_WEB_PORT) { $env:DEEPFIELD_WEB_PORT } else { 8787 }
$Url  = "http://127.0.0.1:$Port"

Set-Location $Repo

function Test-ConsoleUp {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/health" -TimeoutSec 2 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# ── Single instance ──────────────────────────────────────────────────────────
# Two copies double the WebSocket and REST load and race each other's alerts.
# If something already answers on the port, that IS the bot: surface it rather
# than starting a second one.
if (Test-ConsoleUp) {
    Write-Host "DEEPFIELD already running - opening console at $Url"
    if (-not $NoBrowser) { Start-Process $Url }
    exit 0
}

$py = if ($Windowed) { "$Repo\venv\Scripts\python.exe" } else { "$Repo\venv\Scripts\pythonw.exe" }
if (-not (Test-Path $py)) {
    Write-Error "no venv at $py - run: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# A fresh clone has no candle DB and the cold backfill takes ~70s. Do it once,
# visibly, rather than letting the bot start against an empty store.
if (-not (Test-Path "$Repo\deepfield.db")) {
    Write-Host "no candle database - running cold backfill (~70s, first run only)"
    & "$Repo\venv\Scripts\python.exe" -m deepfield --backfill --full
}

$env:DEEPFIELD_EXEC_MODE = $Mode
$env:PYTHONIOENCODING    = "utf-8"   # belt-and-braces; logsetup hardens the handlers too

Write-Host "starting DEEPFIELD (mode=$Mode) ..."
Start-Process -FilePath $py -ArgumentList "-m", "deepfield", "--simple" `
              -WorkingDirectory $Repo -WindowStyle Hidden

# Open the browser once the console answers. Gives up after ~30s rather than
# hanging a login script forever.
if (-not $NoBrowser) {
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-ConsoleUp) { Start-Process $Url; break }
        Start-Sleep -Milliseconds 500
    }
}
