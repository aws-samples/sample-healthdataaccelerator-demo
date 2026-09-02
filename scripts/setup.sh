#!/usr/bin/env bash
set -euo pipefail

echo "=== hdademo Setup ==="

# Initialize and update submodules
echo "Initializing Git submodules..."
git submodule update --init --recursive

# Create virtual environment if not present
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
fi

# Activate and install dependencies
echo "Installing Python dependencies..."
source .venv/bin/activate
pip install -r requirements.txt

echo ""
echo "Setup complete. Activate your environment with:"
echo "  source .venv/bin/activate"
echo ""
echo "Then deploy with:"
echo "  cdk deploy --all"
