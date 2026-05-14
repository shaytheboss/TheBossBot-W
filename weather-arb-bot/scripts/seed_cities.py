"""Seed the 12 Polymarket weather cities into the database.

Run once after migrations:
  python -m scripts.seed_cities

Idempotent — matches existing rows by NAME (not ICAO), so primary_icao can be
updated for cities like NYC (KLGA -> KNYC).

On Railway, cities are seeded automatically at startup via app/utils/seed.py.
This script is for local one-off runs only.
"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO)

from app.utils.seed import seed_cities

if __name__ == "__main__":
    asyncio.run(seed_cities())
