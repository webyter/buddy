#!/usr/bin/env sh
# buddy installer — verifies the machine and launches first-run self-setup.
# usage: git clone https://github.com/webyter/buddy && cd buddy && ./install.sh [--non-interactive] [--no extras]
set -e

echo "installing buddy..."

NONINTERACTIVE=0
for arg in "$@"; do
    case "$arg" in
        --non-interactive|--ci|-y) NONINTERACTIVE=1 ;;
        --help|-h) echo "usage: ./install.sh [--non-interactive]"; exit 0 ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 not found — install Python 3.10+ first."
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "✗ Python 3.10+ required (found $(python3 --version))."
    exit 1
fi
echo "✓ python3 $(python3 --version | cut -d' ' -f2)"

if [ ! -f buddy.py ]; then
    echo "✗ run this from the buddy directory (next to buddy.py)."
    exit 1
fi

# Optional-but-expected platform bits: warn, don't fail.
MISSING=""
command -v git >/dev/null 2>&1 || MISSING="$MISSING git"
command -v systemctl >/dev/null 2>&1 || MISSING="$MISSING systemctl(systemd)"
command -v xdg-open >/dev/null 2>&1 || MISSING="$MISSING xdg-open(desktop)"
if ! python3 -c 'import curses' 2>/dev/null; then
    MISSING="$MISSING python3-curses(TUI)"
fi
if [ -n "$MISSING" ]; then
    echo "! optional missing:$MISSING (buddy still runs; some features degrade)"
fi
if ! python3 -c 'import keyring' 2>/dev/null; then
    echo "! python keyring not installed — API keys fall back to ~/.buddy/secrets.json (0600)"
fi

if [ "$NONINTERACTIVE" = "1" ]; then
    echo
    echo "non-interactive mode: verifying imports only (no setup wizard)."
    python3 -c 'import buddy_core.config, buddy_core.tools, buddy_core.agent; print("✓ imports OK")'
    echo "next: python3 buddy.py setup   # interactive wizard"
    echo "      python3 buddy.py install-service  # 24/7 systemd unit"
    exit 0
fi

echo
echo "first run: buddy will detect your system, offer optional extras,"
echo "ask for your API key (hidden), and verify the connection."
echo

if ! python3 buddy.py; then
    echo "✗ buddy exited with an error — rerun with: python3 buddy.py setup"
    exit 1
fi

echo
echo "tip: python3 buddy.py install-service  # 24/7 daemon (Restart=always)"
