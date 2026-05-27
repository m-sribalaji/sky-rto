# install_win.ps1 - RTO Tracker Windows Installer
# No admin required.
#
# Usage:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#   .\install_win.ps1 -ServerUrl "http://10.131.80.141:8989"

param(
    [string]$ServerUrl = ""
)

$ErrorActionPreference = "Continue"  # non-critical steps continue on error

# -- ADMIN CONFIG - update these before distributing ------
$TeamsWebhook      = "https://skyglobal.webhook.office.com/webhookb2/0d7bdc66-6a73-45fb-a24b-385d4e0cda96@68b865d5-cf18-4b2b-82a4-a4eddb9c5237/IncomingWebhook/a525a07c9ccd4bbc83366d86b04f0302/4ea4908b-0ad1-44e4-982b-b8aa39c45886/V28h2KNMh38ha3v9UR5RdOiiyen2KNykniOao3hJZFQq41"
$TeamsNotifyLevel  = "all"   # all | important | errors
# ---------------------------------------------------------

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir    = Split-Path -Parent $ScriptDir
$ClientDir  = Join-Path $RootDir "client"
$CheckinPy  = Join-Path $ClientDir "checkin.py"

# Support both watcher file names
$WatcherScript = $null
foreach ($name in @("rto_agent_win.py","watch_wake_win.py","rto_agent_win.py")) {
    $candidate = Join-Path $ScriptDir $name
    if (Test-Path $candidate) { $WatcherScript = $candidate; break }
}

$LogDir     = Join-Path $env:USERPROFILE ".rto_tracker"
$ConfigFile = Join-Path $LogDir "config.json"
$RunKey     = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunName    = "SkyRTOTracker"

Write-Host ""
Write-Host "------------------------------------------"
Write-Host "  RTO Tracker - Windows Installer"
Write-Host "------------------------------------------"

# -- Step 1: Python ----------------------------------------
Write-Host ""
Write-Host "  [1/7] Checking Python..."
$PythonExe = $null
foreach ($cmd in @("python","python3")) {
    try {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($c) { $PythonExe = $c.Source; break }
    } catch {}
}
if (-not $PythonExe) {
    Write-Host "    Python not found. Install from https://python.org"
    exit 1
}
$ver = & $PythonExe --version 2>&1
Write-Host "  OK $ver"
Write-Host "  OK checkin.py uses stdlib only - no pip needed"

# -- Step 2: pywin32 ---------------------------------------
Write-Host ""
Write-Host "  [2/7] Checking pywin32 (screen unlock detection)..."
$PyWinOk = $false
try {
    & $PythonExe -c "import win32api" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $PyWinOk = $true }
} catch {}

if (-not $PyWinOk) {
    Write-Host "  Installing pywin32..."
    try {
        & $PythonExe -m pip install pywin32 --user --quiet 2>&1 | Out-Null
        & $PythonExe -c "import win32api" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $PyWinOk = $true
            Write-Host "  OK pywin32 installed"
        } else {
            Write-Host "  ! pywin32 needs restart to activate - using polling fallback"
        }
    } catch {
        Write-Host "  ! pywin32 install failed - using polling fallback (still works)"
    }
} else {
    Write-Host "  OK pywin32 already installed"
}

# -- Step 3: Files -----------------------------------------
Write-Host ""
Write-Host "  [3/7] Checking files..."
if (-not $WatcherScript) {
    Write-Host "    No watcher script found in $ScriptDir"
    Write-Host "    Expected: rto_agent_win.py"
    exit 1
}
if (-not (Test-Path $CheckinPy)) {
    Write-Host "    checkin.py not found at $CheckinPy"; exit 1
}
Write-Host "  OK Watcher: $(Split-Path $WatcherScript -Leaf)"
Write-Host "  OK checkin.py (stdlib only)"

# -- Step 4: Config + reset all caches --------------------
Write-Host ""
Write-Host "  [4/7] Setting up config..."
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Read existing server URL if not passed
if ([string]::IsNullOrWhiteSpace($ServerUrl) -and (Test-Path $ConfigFile)) {
    try {
        $old = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        if ($old.server_url -and $old.server_url -ne "http://YOUR_SERVER_IP:8989") {
            $ServerUrl = $old.server_url
        }
    } catch {}
}
# Prompt if still empty
if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
    Write-Host ""
    Write-Host "  Enter the RTO server URL:"
    Write-Host "  use  http://10.131.80.141:8989"
    $ServerUrl = (Read-Host "  Server URL").Trim().TrimEnd("/")
    if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
        $ServerUrl = "http://YOUR_SERVER_IP:8989"
        Write-Host "  ! No URL entered - edit $ConfigFile later"
    }
}
$ServerUrl = $ServerUrl.TrimEnd("/")

# Always write clean config with ALL caches reset
# Use direct JSON string to avoid BOM and ConvertTo-Json null issues
$ConfigJson = @"
{
  "server_url": "$ServerUrl",
  "teams_webhook": "$TeamsWebhook",
  "teams_notify_level": "$TeamsNotifyLevel",
  "last_checkin_date": null,
  "last_status": null,
  "last_detected_class": null,
  "last_reg_attempt_ts": null,
  "last_reg_attempt_date": null,
  "poll_interval_seconds": 300
}
"@
[System.IO.File]::WriteAllText($ConfigFile, $ConfigJson, (New-Object System.Text.UTF8Encoding $false))

# Verify config written correctly
$verify = Get-Content $ConfigFile -Raw -Encoding UTF8
if ($verify -notmatch [regex]::Escape($ServerUrl)) {
    Write-Host "  RETRY: Config write - using fallback method..."
    $ConfigJson | Out-File -FilePath $ConfigFile -Encoding utf8NoBOM -Force 2>$null
    if (-not $?) {
        # Final fallback: write bytes directly
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($ConfigJson)
        # Strip BOM if present
        if ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            $bytes = $bytes[3..($bytes.Length-1)]
        }
        [System.IO.File]::WriteAllBytes($ConfigFile, $bytes)
    }
}
Write-Host "  OK Config verified: $ServerUrl"

# Clear ALL cache files
@(".checkin.lock", ".last_watcher_run", "pending_queue.json", ".last_missed_check") |
    ForEach-Object { Remove-Item (Join-Path $LogDir $_) -ErrorAction SilentlyContinue }

Write-Host "  OK Server: $ServerUrl"
Write-Host "  OK All caches reset"

# -- Step 5: Auto-start (no admin - HKCU + Startup folder) -
Write-Host ""
Write-Host "  [5/7] Installing auto-start..."

# Build the wpythonw command - use pythonw.exe if available (no console window)
$PythonDir = Split-Path $PythonExe -Parent
$PythonW   = Join-Path $PythonDir "pythonw.exe"
$LaunchExe = if (Test-Path $PythonW) { $PythonW } else { $PythonExe }

$WatcherCmd = "`"$LaunchExe`" `"$WatcherScript`""

# Method A: HKCU Run registry key (fires on every login, no admin)
try {
    New-ItemProperty -Path $RunKey -Name $RunName `
        -Value $WatcherCmd -PropertyType String -Force | Out-Null
    Write-Host "  OK HKCU Run key registered"
} catch {
    Write-Host "  ! HKCU Run key failed: $_"
}

# Method B: Startup folder shortcut (backup, also no admin)
try {
    $StartupFolder = [Environment]::GetFolderPath("Startup")
    $ShortcutPath  = Join-Path $StartupFolder "SkyRTOTracker.lnk"
    $Shell         = New-Object -ComObject WScript.Shell
    $SC            = $Shell.CreateShortcut($ShortcutPath)
    $SC.TargetPath        = $LaunchExe
    $SC.Arguments         = "`"$WatcherScript`""
    $SC.WorkingDirectory  = $ScriptDir
    $SC.WindowStyle       = 7   # minimised
    $SC.Description       = "Sky RTO Tracker Watcher"
    $SC.Save()
    Write-Host "  OK Startup folder shortcut created"
} catch {
    Write-Host "  ! Startup folder shortcut failed: $_"
}

# Clean up old scheduled task if it exists from previous installs
try {
    Unregister-ScheduledTask -TaskName "SkyRTOTracker" -Confirm:$false `
        -ErrorAction SilentlyContinue | Out-Null
    Unregister-ScheduledTask -TaskName "RTO_Tracker_Watcher" -Confirm:$false `
        -ErrorAction SilentlyContinue | Out-Null
} catch {}

# -- Step 6: Server reachability ---------------------------
Write-Host ""
Write-Host "  [6/7] Checking server..."
$ServerOk = $false
try {
    $r = Invoke-WebRequest -Uri "$ServerUrl/health" `
         -Headers @{ "Accept-Encoding" = "identity"; "Connection" = "close" } `
         -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
    $ServerOk = ($r.StatusCode -eq 200)
} catch { $ServerOk = $false }

if ($ServerOk) {
    Write-Host "  OK Server reachable!"
} else {
    Write-Host "  ! Server unreachable at $ServerUrl"
    Write-Host "    Connect VPN if working from home, then run:"
    Write-Host "    python `"$CheckinPy`" --reset"
}

# -- Step 7: Start watcher + registration ------------------
Write-Host ""
Write-Host "  [7/7] Starting watcher + first check-in..."

# Kill any existing watcher instance first
Get-Process -Name "python","pythonw","python3" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*rto_agent*" -or $_.CommandLine -like "*rto_agent_win*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue

# Start watcher as a detached hidden process that survives PowerShell closing
# Using Start-Process with -WindowStyle Hidden + no -Wait = fully detached
$WatcherArgs = "`"$WatcherScript`""
Start-Process -FilePath $LaunchExe `
              -ArgumentList $WatcherArgs `
              -WindowStyle Hidden `
              -WorkingDirectory $ScriptDir
Write-Host "  OK Watcher started (survives session close)"

# Give watcher a moment to initialise
Start-Sleep -Seconds 5

if ($ServerOk) {
    # Check registration status
    $Hostname = $env:COMPUTERNAME.ToUpper()
    $IsRegistered = $false
    $RegName = ""
    try {
        $resp = Invoke-WebRequest -Uri "$ServerUrl/api/device/$Hostname" `
                -Headers @{ "Accept-Encoding" = "identity"; "Connection" = "close" } `
                -UseBasicParsing -TimeoutSec 5
        $dev  = $resp.Content | ConvertFrom-Json
        $IsRegistered = $dev.registered
        $RegName = $dev.employee_name
    } catch { $IsRegistered = $false }

    if ($IsRegistered) {
        Write-Host "  OK Already registered as: $RegName"
        Write-Host "  Running first check-in..."
        & $PythonExe $CheckinPy --reset
        Write-Host ""
        Write-Host "  Result:"
        $logPath = Join-Path $LogDir "checkin.log"
        if (Test-Path $logPath) {
            Get-Content $logPath -Tail 5 |
                Where-Object { $_ -match "Signals|action=|Checked in|wfh|wfo|already" } |
                ForEach-Object { Write-Host "    $_" }
        }
    } else {
        # Not registered - open browser and wait for completion
        Write-Host "  Opening registration page in browser..."
        $RegUrl = "$ServerUrl/register/$Hostname"

        # Use Start-Process to open browser reliably
        Start-Process $RegUrl

        Write-Host "  Browser opened > fill in name + employee ID > click Register"
        Write-Host ""
        Write-Host "  Waiting for registration (up to 2 minutes)..."

        $waitCount = 0
        $regDone   = $false
        while ($waitCount -lt 40) {
            Start-Sleep -Seconds 3
            $waitCount++
            try {
                $chk  = Invoke-WebRequest -Uri "$ServerUrl/api/device/$Hostname" `
                        -Headers @{ "Accept-Encoding" = "identity"; "Connection" = "close" } `
                        -UseBasicParsing -TimeoutSec 5
                $chkD = $chk.Content | ConvertFrom-Json
                if ($chkD.registered) {
                    $regDone = $true
                    $RegName = $chkD.employee_name
                    break
                }
            } catch {}
            if ($waitCount % 5 -eq 0) {
                Write-Host "    Still waiting... ($($waitCount * 3)s)"
            }
        }

        if ($regDone) {
            Write-Host "  OK Registered as: $RegName"
            Write-Host "  Running first check-in..."
            # Clear reg attempt timestamp so checkin runs cleanly
            try {
                $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
                $cfg.last_reg_attempt_ts = $null
                $cfgJson = $cfg | ConvertTo-Json -Depth 5
                [System.IO.File]::WriteAllText($ConfigFile, $cfgJson, (New-Object System.Text.UTF8Encoding $false))
            } catch {}
            & $PythonExe $CheckinPy --force
        } else {
            Write-Host "  ! Registration not completed in 2 minutes"
            Write-Host "    After registering, run:"
            Write-Host "    python `"$CheckinPy`" --reset"
        }
    }
} else {
    Write-Host "  ! Skipping check-in - server unreachable"
    Write-Host "    Connect VPN and run: python `"$CheckinPy`" --reset"
}

Write-Host ""
Write-Host "------------------------------------------"
Write-Host "  OK RTO Tracker installed!"
Write-Host ""
Write-Host "  Triggers: screen unlock + every 5 min"
Write-Host "  Auto-starts: on every Windows login"
Write-Host ""
Write-Host "  Logs  : $LogDir\rto_agent.log"
Write-Host "  Reset : python `"$CheckinPy`" --reset"
Write-Host "  Force : python `"$CheckinPy`" --force"
Write-Host "  Server: $ServerUrl"
Write-Host "------------------------------------------"
Write-Host ""
