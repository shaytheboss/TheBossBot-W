#!/bin/bash
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Seeding cities..."
python -m scripts.seed_cities

echo "Starting scheduler in background..."
python -m app.workers.scheduler &
SCHEDULER_PID=$!

echo "Starting FastAPI server (Telegram webhook integrated)..."
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

# If uvicorn exits, kill background jobs
kill $SCHEDULER_PID 2>/dev/null || true
