#!/bin/bash
# RuGPT Engine Setup Script
# Creates virtual environment and installs dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==================================="
echo "RuGPT Engine Setup"
echo "==================================="

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Virtual environment created."
else
    echo "Virtual environment already exists."
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo ".env file created. Please edit it with your settings."
else
    echo ".env file already exists."
fi

echo ""
echo "==================================="
echo "Setup complete!"
echo "==================================="
echo ""
echo "To activate the virtual environment:"
echo "  source venv/bin/activate"
echo ""
echo "To run database migrations:"
echo "  python -m src.engine.migrations.migrate"
echo ""
echo "To start the engine:"
echo "  python -m src.engine.run"
echo ""
echo "Or with uvicorn directly:"
echo "  uvicorn src.engine.app:app --host 127.0.0.1 --port 8000"
echo ""
