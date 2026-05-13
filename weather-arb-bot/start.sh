#!/bin/bash
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Seeding cities..."
python -m scripts.seed_cities || echo "WARNING: seed_cities failed or skipped"

echo "Starting FastAPI server (scheduler + Telegram webhook integrated)..."
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
