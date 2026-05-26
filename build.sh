#!/bin/bash
# build.sh - Sky RTO Tracker binary builder
#
# Produces:
#   dist/rto-mac-arm64        macOS Apple Silicon (M1/M2/M3)
#   dist/rto-mac-x86          macOS Intel
#   dist/rto-win.exe          Windows x64  (run on Windows or via GitHub Actions)
#
# Requirements (run once):
#   pip3 install pyinstaller pyobjc-framework-Cocoa certifi   # macOS
#   pip install pyinstaller pywin32 certifi                   # Windows
#
# Usage:
#   chmod +x build.sh
#   ./build.sh                    # build for current arch
#   ./build.sh --all-mac          # build both arm64 + x86 (macOS only)
#   ./build.sh --windows          # reminder to run on Windows
#
# The resulting binaries in dist/ are fully self-contained.
# Copy them to your distribution folder and share.
# ------------------------------------------------------------------------------

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$SCRIPT_DIR/client"
INSTALLER_DIR="$SCRIPT_DIR/scripts"
DIST_DIR="$SCRIPT_DIR/dist"

# Detect platform
IS_MAC=false
IS_WIN=false
case "$(uname -s)" in
    Darwin*) IS_MAC=true ;;
    MINGW*|MSYS*|CYGWIN*) IS_WIN=true ;;
esac

ARCH=$(uname -m)  # arm64 or x86_64

mkdir -p "$DIST_DIR"

echo ""
echo "============================================"
echo "  Sky RTO Tracker - Binary Builder"
echo "============================================"
echo ""
echo "  Platform : $(uname -s) / $ARCH"
echo "  Dist dir : $DIST_DIR"
echo ""

# ── Sanity checks ──────────────────────────────────────────────────────────
# Find pyinstaller — try PATH first, then python3 -m (handles user installs)
PYINSTALLER=""
if command -v pyinstaller &>/dev/null; then
    PYINSTALLER="pyinstaller"
elif python3 -m PyInstaller --version &>/dev/null 2>&1; then
    PYINSTALLER="python3 -m PyInstaller"
else
    # Last resort: check common user install paths
    USER_BIN="$HOME/Library/Python/3.14/bin/pyinstaller"
    if [ -f "$USER_BIN" ]; then
        PYINSTALLER="$USER_BIN"
    fi
fi

if [ -z "$PYINSTALLER" ]; then
    echo "  [FAIL] pyinstaller not found."
    echo "  Run:  pip3 install pyinstaller"
    if [ "$IS_MAC" = true ]; then
        echo "  Also: pip3 install pyobjc-framework-Cocoa"
    fi
    exit 1
fi
echo "  PyInstaller : $PYINSTALLER"
echo ""

# Wrapper so we can call $PYINSTALLER as a command with flags
function run_pyinstaller() {
    if [[ "$PYINSTALLER" == "python3 -m PyInstaller" ]]; then
        python3 -m PyInstaller "$@"
    else
        "$PYINSTALLER" "$@"
    fi
}

if [ ! -f "$CLIENT_DIR/checkin_core.py" ]; then
    echo "  [FAIL] client/checkin_core.py not found at $CLIENT_DIR"
    exit 1
fi

if [ ! -f "$INSTALLER_DIR/rto_agent_mac.py" ] && [ "$IS_MAC" = true ]; then
    echo "  [FAIL] scripts/rto_agent_mac.py not found at $INSTALLER_DIR"
    exit 1
fi

# ── Common PyInstaller flags ────────────────────────────────────────────────
# --onefile      : single binary, no folder
# --noconfirm    : overwrite existing dist without asking
# --clean        : clean build cache before building
# --strip        : strip debug symbols (smaller binary)
# --noupx        : skip UPX compression (avoids false-positive AV alerts)

COMMON_FLAGS=(
    --onefile
    --noconfirm
    --clean
    --noupx
    --distpath "$DIST_DIR"
    --workpath "$SCRIPT_DIR/build"
    --specpath "$SCRIPT_DIR/build"
    # Bundle checkin_core and notifier alongside the agent
    --add-data "$CLIENT_DIR/checkin_core.py:."
    --add-data "$CLIENT_DIR/notifier.py:."
    # Hidden imports that PyInstaller may miss
    --hidden-import=checkin_core
    --hidden-import=notifier
    --hidden-import=platform
    --hidden-import=ipaddress
    --hidden-import=urllib.request
    --hidden-import=urllib.error
    --hidden-import=fcntl
    --hidden-import=json
    --hidden-import=threading
    --hidden-import=socket
    --hidden-import=argparse
    --hidden-import=logging
    --hidden-import=webbrowser
    --hidden-import=random
    --hidden-import=datetime
    --hidden-import=pathlib
    --hidden-import=ssl
    --hidden-import=certifi
    --collect-all=certifi
)

# ── macOS build ─────────────────────────────────────────────────────────────
if [ "$IS_MAC" = true ]; then
    # Ensure certifi is available for SSL in the binary
    python3 -m pip install certifi --quiet --user 2>/dev/null || true
    echo "  [1/1] Building macOS binary..."
    echo ""

    # Determine output name based on architecture
    if [ "$ARCH" = "arm64" ]; then
        OUT_NAME="rto-mac-arm64"
    else
        OUT_NAME="rto-mac-x86"
    fi

    # macOS-specific: bundle PyObjC for NSWorkspace screen-unlock notifications
    MAC_FLAGS=(
        --hidden-import=AppKit
        --hidden-import=Foundation
        --hidden-import=objc
    )

    run_pyinstaller \
        "${COMMON_FLAGS[@]}" \
        "${MAC_FLAGS[@]}" \
        --name "$OUT_NAME" \
        "$INSTALLER_DIR/rto_agent_mac.py"

    echo ""
    if [ -f "$DIST_DIR/$OUT_NAME" ]; then
        chmod +x "$DIST_DIR/$OUT_NAME"
        SIZE=$(du -sh "$DIST_DIR/$OUT_NAME" | cut -f1)
        echo "  [OK] $OUT_NAME  ($SIZE)"
        echo ""
        echo "  Distribution: copy dist/$OUT_NAME to employees"
        echo "  Install:      double-click or ./rto-mac-arm64 in Terminal"
    else
        echo "  [FAIL] Binary not found in dist/"
        exit 1
    fi

    # Optional: build x86 as well if --all-mac passed and Rosetta available
    if [[ "$*" == *"--all-mac"* ]] && [ "$ARCH" = "arm64" ]; then
        echo ""
        echo "  Building x86_64 (via arch -x86_64)..."
        arch -x86_64 pyinstaller \
            "${COMMON_FLAGS[@]}" \
            "${MAC_FLAGS[@]}" \
            --name "rto-mac-x86" \
            "$INSTALLER_DIR/rto_agent_mac.py" || \
            echo "  [WARN] x86_64 build failed - needs Rosetta or x86 machine"
    fi

# ── Windows build ────────────────────────────────────────────────────────────
elif [ "$IS_WIN" = true ]; then
    echo "  [1/1] Building Windows binary..."
    echo ""

    WIN_FLAGS=(
        # Run as a windowed app (no console flash on startup)
        # Comment out --windowed if you want a console window for debugging
        --windowed
        --hidden-import=winreg
        --hidden-import=ctypes
        --hidden-import=ctypes.windll
        --hidden-import=ctypes.wintypes
        # pywin32 optional - include if available
        --hidden-import=win32api
        --hidden-import=win32con
        --hidden-import=win32gui
        --hidden-import=win32ts
    )

    # Remove --windowed for --force/--reset/--retry (they print to console)
    # Use a wrapper spec or build two variants if needed.
    # For now: single binary, console visible only when run from cmd/PS.
    WIN_FLAGS_ADJUSTED=("${WIN_FLAGS[@]/--windowed/}")

    run_pyinstaller \
        "${COMMON_FLAGS[@]}" \
        "${WIN_FLAGS_ADJUSTED[@]}" \
        --name "rto-win" \
        "$INSTALLER_DIR/rto_agent_win.py"

    echo ""
    if [ -f "$DIST_DIR/rto-win.exe" ]; then
        echo "  [OK] rto-win.exe built"
        echo ""
        echo "  Distribution: copy dist/rto-win.exe to employees"
        echo "  Install:      double-click rto-win.exe"
    else
        echo "  [FAIL] Binary not found in dist/"
        exit 1
    fi

else
    echo "  [INFO] Not on macOS or Windows."
    echo "  Run this script on:"
    echo "    - A Mac (arm64) to build rto-mac-arm64"
    echo "    - A Mac (Intel) to build rto-mac-x86"
    echo "    - A Windows machine to build rto-win.exe"
    echo ""
    echo "  Or use GitHub Actions (see below):"
    echo ""
    echo "  .github/workflows/build.yml — triggers on push to main"
    echo "  Produces all 3 binaries as release artifacts."
    echo ""
    echo "  To create the workflow:"
    echo "    mkdir -p .github/workflows"
    echo "    cp build-github-actions.yml .github/workflows/build.yml"
fi

echo ""
echo "============================================"
echo "  Build complete"
echo "============================================"
echo ""
echo "  Files in dist/:"
ls -lh "$DIST_DIR/" 2>/dev/null || echo "  (empty)"
echo ""