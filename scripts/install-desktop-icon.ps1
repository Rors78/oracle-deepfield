# Create the ORACLE DEEPFIELD desktop icon, and optionally the autostart entry.
#
# Windows equivalent of dropping scripts/DEEPFIELD.desktop into ~/.local/share/
# applications. Writes .lnk shortcuts that run deepfield-desktop.ps1 through
# powershell -WindowStyle Hidden, so a launch shows the browser and the bot, not
# a console window.
#
#   .\scripts\install-desktop-icon.ps1                 # desktop icon only
#   .\scripts\install-desktop-icon.ps1 -Autostart      # + run at every login
#   .\scripts\install-desktop-icon.ps1 -Uninstall      # remove both
#
# Both shortcuts launch in paper mode (the launcher's default). Nothing here
# arms trading.

param(
    [switch]$Autostart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Repo     = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $Repo "scripts\deepfield-desktop.ps1"
$Desktop  = [Environment]::GetFolderPath('Desktop')
$Startup  = [Environment]::GetFolderPath('Startup')
$Name     = "ORACLE DEEPFIELD.lnk"

$targets = @((Join-Path $Desktop $Name))
if ($Autostart -or $Uninstall) { $targets += (Join-Path $Startup $Name) }

if ($Uninstall) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "removed $t" }
        else { Write-Host "not present $t" }
    }
    exit 0
}

if (-not (Test-Path $Launcher)) { Write-Error "launcher missing: $Launcher"; exit 1 }

# Icon: use the repo's own if one was added, else fall back to a stock shell
# icon so the shortcut never renders blank.
$icon = Join-Path $Repo "scripts\deepfield.ico"
if (-not (Test-Path $icon)) { $icon = "$env:SystemRoot\System32\shell32.dll,13" }

$shell = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
    $sc = $shell.CreateShortcut($t)
    $sc.TargetPath  = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    # -WindowStyle Hidden so no console flashes; -File so the path may contain spaces.
    $sc.Arguments   = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
    $sc.WorkingDirectory = $Repo
    $sc.IconLocation = $icon
    $sc.Description  = "ORACLE DEEPFIELD - Kraken cycle-bottom monitor (paper mode)"
    $sc.Save()
    Write-Host "created $t"
}

Write-Host ""
Write-Host "mode      : paper (no orders, no private Kraken calls)"
Write-Host "console   : http://127.0.0.1:8787"
if ($Autostart) { Write-Host "autostart : yes - runs at every login" }
else            { Write-Host "autostart : no  - re-run with -Autostart to enable" }
