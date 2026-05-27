#!/bin/bash
# install_macos.sh - RTO Tracker macOS installer
# No sudo/admin required.

set -e

# -- ADMIN CONFIG - update these before distributing ------
TEAMS_WEBHOOK="https://skyglobal.webhook.office.com/webhookb2/0d7bdc66-6a73-45fb-a24b-385d4e0cda96@68b865d5-cf18-4b2b-82a4-a4eddb9c5237/IncomingWebhook/a525a07c9ccd4bbc83366d86b04f0302/4ea4908b-0ad1-44e4-982b-b8aa39c45886/V28h2KNMh38ha3v9UR5RdOiiyen2KNykniOao3hJZFQq41"
TEAMS_NOTIFY_LEVEL="all"   # all | important | errors
# ---------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$(cd "$SCRIPT_DIR/../client" && pwd)"
CHECKIN_SCRIPT="$CLIENT_DIR/checkin.py"
PLIST_SRC="$SCRIPT_DIR/com.sky.rto.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sky.rto.plist"
LOG_DIR="$HOME/.rto_tracker"
SERVER_URL="${RTO_SERVER_URL:-}"

echo ""
echo "------------------------------------------"
echo "  RTO Tracker - macOS Installer"
echo "------------------------------------------"

# -- Step 1: Check Python3 ----------------------
echo ""
echo "  [1/6] Checking Python..."
if ! command -v python3 &>/dev/null; then
  echo "    python3 not found. Install from https://python.org"
  exit 1
fi
PYTHON=$(command -v python3)
echo "    $($PYTHON --version)"
echo "    checkin.py uses stdlib only - no pip needed"

# -- Step 2: Auto-install PyObjC ---------------
echo ""
echo "  [2/6] Checking PyObjC (screen unlock detection)..."
if "$PYTHON" -c "from AppKit import NSWorkspace" 2>/dev/null; then
  echo "    PyObjC already installed"
else
  echo "  Installing PyObjC..."
  if "$PYTHON" -m pip install pyobjc-framework-Cocoa \
      --user --quiet --no-warn-script-location 2>/dev/null; then
    if "$PYTHON" -c "from AppKit import NSWorkspace" 2>/dev/null; then
      echo "    PyObjC installed"
    else
      echo "  WARNING PyObjC installed - needs new terminal to activate"
      echo "    Using CGSession fallback (still works)"
    fi
  else
    echo "  WARNING PyObjC install failed - using CGSession fallback"
  fi
fi

# -- Step 3: Find watcher script ---------------
echo ""
echo "  [3/6] Checking files..."

WATCHER_SCRIPT=""
for name in "rto_agent_mac.py" "rto_agent_mac.py" "watch_wake_mac.py"; do
  if [ -f "$SCRIPT_DIR/$name" ]; then
    WATCHER_SCRIPT="$SCRIPT_DIR/$name"
    echo "    Watcher script: $name"
    break
  fi
done

if [ -z "$WATCHER_SCRIPT" ]; then
  echo "    No watcher script found in $SCRIPT_DIR"
  echo "    Expected: rto_agent_mac.py"
  exit 1
fi

if [ ! -f "$CHECKIN_SCRIPT" ]; then
  echo "    checkin.py not found at $CHECKIN_SCRIPT"
  exit 1
fi
echo "    checkin.py (stdlib only - no dependencies)"

# -- Step 4: Setup config + reset ALL caches --
echo ""
echo "  [4/6] Setting up config..."
mkdir -p "$LOG_DIR"

if [ ! -f "$LOG_DIR/config.json" ] || [ -z "$SERVER_URL" ]; then
  # Fresh install or SERVER_URL passed via env - get the URL
  if [ -z "$SERVER_URL" ]; then
    # Try reading from existing config first
    SERVER_URL=$("$PYTHON" -c "
import json, sys
try:
    data = json.load(open('$LOG_DIR/config.json'))
    url = data.get('server_url','')
    if url and url != 'http://YOUR_SERVER_IP:8989':
        print(url)
    else:
        print('')
except:
    print('')
" 2>/dev/null || echo "")
  fi

  if [ -z "$SERVER_URL" ]; then
    echo ""
    echo "  -------------------------------------------"
    echo "  -  Enter the  RTO server address:         -"
    echo "  -  use  http://10.131.80.141:8989         -"
    echo "  -------------------------------------------"
    read -rp "  Server URL: " SERVER_URL
    SERVER_URL="${SERVER_URL%/}"
    if [ -z "$SERVER_URL" ]; then
      SERVER_URL="http://YOUR_SERVER_IP:8989"
      echo "  WARNING No URL entered - edit $LOG_DIR/config.json later"
    fi
  fi
else
  # Read existing URL
  SERVER_URL=$("$PYTHON" -c "
import json
data = json.load(open('$LOG_DIR/config.json'))
print(data.get('server_url','unknown'))
" 2>/dev/null || echo "unknown")
fi

# Always write a clean config with all caches reset
# This runs on EVERY install - fresh or reinstall
cat > "$LOG_DIR/config.json" << JSON
{
  "server_url": "$SERVER_URL",
  "teams_webhook": "$TEAMS_WEBHOOK",
  "teams_notify_level": "$TEAMS_NOTIFY_LEVEL",
  "last_checkin_date": null,
  "last_status": null,
  "last_detected_class": null,
  "last_reg_attempt_ts": null,
  "last_reg_attempt_date": null,
  "poll_interval_seconds": 300
}
JSON

# Always clear ALL cache files
rm -f "$LOG_DIR/.last_watcher_run"   2>/dev/null || true
rm -f "$LOG_DIR/.checkin.lock"       2>/dev/null || true
rm -f "$LOG_DIR/pending_queue.json"  2>/dev/null || true

echo "    Server: $SERVER_URL"
echo "    All caches reset (fresh start)"
echo "    Cleared: config cache, watcher dedup, lock file, offline queue"


# -- Step 5: Install launchd agent -------------
echo ""
echo "  [5/6] Installing LaunchAgent (screen unlock trigger)..."
mkdir -p "$HOME/Library/LaunchAgents"

sed \
  -e "s|PYTHON_PATH|$PYTHON|g" \
  -e "s|WATCHER_PATH|$WATCHER_SCRIPT|g" \
  -e "s|LOG_DIR|$LOG_DIR|g" \
  "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

# Wait longer for watcher to start and complete its startup check-in run
# This prevents the installer's step 6 from racing with the watcher
# and opening the registration browser twice
echo "  Waiting for watcher to initialise..."
sleep 5

if launchctl list | grep -q "com.sky.rto"; then
  echo "    LaunchAgent loaded - watcher is running"
  echo "    Plist: $PLIST_DST"
  echo "    Watcher: $WATCHER_SCRIPT"
else
  echo "  WARNING Agent may not be running"
  echo "    Check: $LOG_DIR/launchd_err.log"
fi

# -- Step 6: Register + first check-in ---------
echo ""
echo "  [6/6] Device registration + first check-in..."
echo ""

# Check server reachable
SERVER_OK=false
if "$PYTHON" -c "
import urllib.request, sys
try:
    req = urllib.request.Request(
        '$SERVER_URL/health',
        headers={'Accept-Encoding': 'identity', 'Connection': 'close'},
    )
    urllib.request.urlopen(req, timeout=8)
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null; then
  SERVER_OK=true
fi

if [ "$SERVER_OK" = false ]; then
  echo "  WARNING Server not reachable at $SERVER_URL"
  echo ""
  echo "  This is normal if:"
  echo "    > You're at home and VPN is off"
  echo "    > Server is still starting up"
  echo ""
  echo "  ------------------------------------------"
  echo "  ACTION NEEDED after connecting VPN:"
  echo ""
  echo "    python3 $CHECKIN_SCRIPT --reset"
  echo ""
  echo "  Or just unlock your screen - it fires"
  echo "  automatically once server is reachable!"
  echo "  ------------------------------------------"
else
  echo "    Server reachable!"

  # Check if already registered
  HOSTNAME=$(hostname | tr '[:lower:]' '[:upper:]')
  REGISTERED=$("$PYTHON" -c "
import urllib.request, json, sys
try:
    req = urllib.request.Request(
        '$SERVER_URL/api/device/$HOSTNAME',
        headers={'Accept-Encoding': 'identity', 'Connection': 'close'},
    )
    r = urllib.request.urlopen(req, timeout=5)
    d = json.loads(r.read())
    print('yes:' + d.get('employee_name','') if d.get('registered') else 'no')
except Exception as e:
    print('error:' + str(e))
" 2>/dev/null || echo "error")

  if [[ "$REGISTERED" == yes:* ]]; then
    # Already registered - run --reset which clears cache + fires check-in
    NAME="${REGISTERED#yes:}"
    echo "    Registered as: $NAME"
    echo "  Running first check-in now..."
    echo ""
    "$PYTHON" "$CHECKIN_SCRIPT" --reset
    echo ""
    echo "  Result:"
    tail -5 "$LOG_DIR/checkin.log" 2>/dev/null | \
      grep -E "(Signals|action=|Checked in|wfh|wfo|already)" | \
      sed 's/^.*\[INFO\] /    /' || echo "    (check $LOG_DIR/checkin.log)"

  else
    # Not registered - open browser and wait for completion (up to 2 mins)
    echo "    Device not yet registered"
    echo "  Opening registration page in browser..."
    echo ""
    open "$SERVER_URL/register/$HOSTNAME" 2>/dev/null || \
      "$PYTHON" -m webbrowser "$SERVER_URL/register/$HOSTNAME" 2>/dev/null || true

    echo "  Browser opened -> fill in name + employee ID -> click Register"
    echo ""
    echo "    Waiting for registration to complete..."
    echo "     (watching for up to 2 minutes)"
    echo ""

    # Poll every 3 seconds for up to 2 minutes (40 attempts)
    WAIT_COUNT=0
    REGISTERED_NOW=false
    while [ $WAIT_COUNT -lt 40 ]; do
      sleep 3
      WAIT_COUNT=$((WAIT_COUNT + 1))

      CHECK=$("$PYTHON" -c "
import urllib.request, json, sys
try:
    req = urllib.request.Request(
        '$SERVER_URL/api/device/$HOSTNAME',
        headers={'Accept-Encoding': 'identity', 'Connection': 'close'},
    )
    r = urllib.request.urlopen(req, timeout=5)
    d = json.loads(r.read())
    print('yes:' + d.get('employee_name','') if d.get('registered') else 'no')
except:
    print('no')
" 2>/dev/null || echo "no")

      if [[ "$CHECK" == yes:* ]]; then
        NAME="${CHECK#yes:}"
        REGISTERED_NOW=true
        break
      fi

      # Show progress every 5 checks (~15 seconds)
      if [ $((WAIT_COUNT % 5)) -eq 0 ]; then
        echo "    Still waiting... ($((WAIT_COUNT * 3))s elapsed)"
      fi
    done

    if [ "$REGISTERED_NOW" = true ]; then
      echo "  Registered as: $NAME"
      echo "  Running first check-in now..."
      echo ""
      # --reset clears caches + fires check-in in one call
      "$PYTHON" "$CHECKIN_SCRIPT" --reset
      echo ""
      echo "  Result:"
      tail -5 "$LOG_DIR/checkin.log" 2>/dev/null | \
        grep -E "(Signals|action=|Checked in|wfh|wfo|already)" | \
        sed 's/^.*\[INFO\] /    /' || echo "    (check $LOG_DIR/checkin.log)"
    else
      echo "  WARNING Registration not completed within 2 minutes."
      echo "  Run this after registering:"
      echo ""
      echo "    python3 $CHECKIN_SCRIPT --reset"
    fi
  fi
fi

# -- Done --------------------------------------
echo ""
echo "------------------------------------------"
echo "  RTO Tracker installed!"
echo ""
echo "  Every screen unlock -> auto check-in"
echo "  No action needed from you daily."
echo ""
echo "  Logs  : tail -f $LOG_DIR/rto_agent.log"
echo "  Reset : python3 $CHECKIN_SCRIPT --reset"
echo "  Force : python3 $CHECKIN_SCRIPT --force"
echo "  Server: $SERVER_URL"
echo "------------------------------------------"
echo ""
