#!/bin/bash
###############################################################################
# HoneyPrint Quick Start Script
#
# Starts a HoneyPrint instance with default configuration.
# Analogous to HoneySat's run-experiment-csp.sh
#
# Usage:
#   ./run_honeyprint.sh                    # Default Ender-3 V2
#   ./run_honeyprint.sh prusa_mk4          # Prusa MK4 profile
#   ./run_honeyprint.sh bambu_x1c          # Bambu X1C profile
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PROFILE="${1:-ender3_v2}"

echo "============================================================"
echo "  HoneyPrint - Additive Manufacturing Honeypot"
echo "  Profile: $PROFILE"
echo "============================================================"

# Set environment variables
export HONEYPRINT_PROFILE="$PROFILE"

# Check for Docker
if ! command -v docker &>/dev/null; then
    echo "Docker not found. Running in local mode..."
    echo ""
    cd "$PROJECT_DIR"

    # Check Python
    if ! command -v python3 &>/dev/null; then
        echo "Error: Python 3 is required."
        exit 1
    fi

    # Create venv if needed
    if [ ! -d "venv" ]; then
        echo "Creating virtual environment..."
        python3 -m venv venv
        source venv/bin/activate
        pip install -r requirements.txt
    else
        source venv/bin/activate
    fi

    echo "Starting HoneyPrint locally..."
    echo "  Web:    http://localhost:8080"
    echo "  Telnet: telnet localhost 2323"
    echo ""
    echo "Press Ctrl+C to stop."
    echo ""

    HONEYPRINT_WEB_PORT=8080 \
    HONEYPRINT_TELNET_PORT=2323 \
    HONEYPRINT_MONGODB_URI="mongodb://localhost:27017" \
    python -m honeyprint.main --profile "$PROFILE" \
        --web-port 8080 --telnet-port 2323

else
    echo "Starting with Docker Compose..."
    cd "$PROJECT_DIR"
    docker compose up --build -d
    echo ""
    echo "HoneyPrint is running!"
    echo "  Web:    http://localhost:80"
    echo "  Telnet: telnet localhost 23"
    echo ""
    echo "View logs: docker compose logs -f honeyprint"
    echo "Stop:      docker compose down"
fi
