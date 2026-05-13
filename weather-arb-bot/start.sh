#!/bin/bash
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting workers and bot in background..."
python -m app.workers.scheduler &
SCHEDULER_PID=$!

python -m app.bot.telegram_bot &
BOT_PID=$!

echo "Starting FastAPI server..."
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

# If uvicorn exits, kill background jobs
kill $SCHEDULER_PID $BOT_PID 2>/dev/null || true
