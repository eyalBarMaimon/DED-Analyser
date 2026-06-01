#!/bin/bash
# DED Analyser — Mac Launcher
# Double-click this file to start the app

# Change to the folder where this script lives
cd "$(dirname "$0")"

echo "=============================="
echo "  Meltio DED Analyser"
echo "=============================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    osascript -e 'display dialog "Python 3 is not installed.\n\nPlease install it from python.org and try again." buttons {"OK"} with icon stop'
    exit 1
fi

PYTHON=$(command -v python3)
echo "Python: $PYTHON"
echo ""

# Install requirements if needed
if [ -f "requirements.txt" ]; then
    echo "Checking requirements..."
    "$PYTHON" -m pip install -r requirements.txt --quiet
    echo "Requirements OK"
    echo ""
fi

# Open browser after short delay
(sleep 2 && open "http://localhost:5050/ded") &

# Run the app
echo "Starting server..."
echo "Browser will open automatically at http://localhost:5050/ded"
echo ""
"$PYTHON" app.py
