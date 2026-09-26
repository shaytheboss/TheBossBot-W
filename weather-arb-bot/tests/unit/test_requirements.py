"""The production dependency pins.

A deploy on 2026-09-26 failed at `alembic upgrade head`: the image rebuilt,
pip pulled SQLAlchemy 2.1.1, which no longer installs greenlet by default,
and `sqlalchemy.ext.asyncio` refused to import. Nothing in the code changed.
The dev environment still had greenlet, so no test could see it — these read
the file the Docker image is built from.
"""
from __future__ import annotations

import re
from pathlib import Path

REQ = Path(__file__).resolve().parents[2] / "requirements.txt"


def _spec(name: str) -> str:
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if re.match(rf"{re.escape(name)}(\[|[<>=!~ ]|$)", line, re.IGNORECASE):
            return line
    raise AssertionError(f"{name} is not in requirements.txt")


def test_sqlalchemy_brings_greenlet():
    assert "[asyncio]" in _spec("sqlalchemy"), \
        "without the asyncio extra SQLAlchemy 2.1+ installs no greenlet"


def test_sqlalchemy_stays_on_the_tested_series():
    assert "<2.1" in _spec("sqlalchemy")


def test_apscheduler_stays_below_the_4_rewrite():
    assert "<4" in _spec("apscheduler")
